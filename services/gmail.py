"""Gmail integration — OAuth2 auth, send email, fetch inbox. Multi-account."""

import base64
import logging
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import db

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

_PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "http://localhost:8000")
FALLBACK_REDIRECT_URI = os.environ.get(
    "GMAIL_CALLBACK_URL", f"{_PORTAL_BASE}/settings/email/callback/gmail"
)


def _redirect_uri(account: dict) -> str:
    return (account.get("callback_url") or "").strip() or FALLBACK_REDIRECT_URI


def _build_flow(account: dict) -> Flow:
    redirect_uri = _redirect_uri(account)
    client_config = {
        "web": {
            "client_id": account["client_id"],
            "client_secret": account["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def get_auth_url(account_id: int) -> str | None:
    account = db.get_email_account(account_id)
    if not account or not account["client_id"] or not account["client_secret"]:
        return None
    flow = _build_flow(account)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=str(account_id),
    )
    return auth_url


def exchange_code(code: str, account_id: int) -> bool:
    account = db.get_email_account(account_id)
    if not account or not account["client_id"] or not account["client_secret"]:
        return False
    flow = _build_flow(account)
    flow.fetch_token(code=code)
    creds = flow.credentials
    expiry = creds.expiry or datetime.now(timezone.utc)
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    db.save_email_tokens(account_id, email, creds.token, creds.refresh_token, expiry)
    return True


def _get_credentials(account: dict) -> Credentials | None:
    if not account or not account["access_token"] or not account["refresh_token"]:
        return None
    creds = Credentials(
        token=account["access_token"],
        refresh_token=account["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        scopes=SCOPES,
    )
    if account["token_expiry"]:
        expiry = account["token_expiry"]
        if hasattr(expiry, "replace"):
            if expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)
        creds.expiry = expiry
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        db.update_email_access_token(account["id"], creds.token, creds.expiry)
    return creds


def send_email(task_id: int) -> bool:
    """Send the approved email draft for a task (portal approval flow)."""
    task = db.get_task(task_id)
    if not task:
        logger.error("send_email: task %d not found", task_id)
        return False
    if task["email_status"] != "approved":
        logger.error("send_email: task %d email not approved (status=%s)", task_id, task["email_status"])
        return False
    if not task["contact_email"]:
        logger.error("send_email: task %d has no contact email", task_id)
        return False

    account = db.get_account_for_contact(task.get("contact_id"))
    if not account or account.get("provider") != "gmail":
        # Fall back to first connected Gmail account
        gmail_accounts = db.get_connected_gmail_accounts()
        account = gmail_accounts[0] if gmail_accounts else None
    if not account:
        logger.error("send_email: no connected Gmail account")
        return False

    creds = _get_credentials(account)
    if not creds:
        logger.error("send_email: could not load credentials for account %d", account["id"])
        return False

    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["To"] = task["contact_email"]
    msg["Subject"] = task["email_subject"] or task["name"]
    msg.attach(MIMEText(task["email_body"] or "", "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

    db.update_task_fields(task_id, email_status="sent", sent_at=datetime.now(timezone.utc))
    logger.info("Sent email for task %d to %s", task_id, task["contact_email"])
    return True


def send_email_to_contact(
    account: dict,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    summary: str = "",
    contact_id: int = None,
    task_id: int = None,
) -> str | None:
    """Send an email via Gmail. Returns provider_message_id or None on error."""
    creds = _get_credentials(account)
    if not creds:
        logger.error("send_email_to_contact: could not load credentials for account %d", account["id"])
        return None

    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    msg_id = result.get("id")

    db.log_email(to_name, to_email, subject, summary, msg_id, contact_id, task_id,
                 email_account_id=account["id"])

    if task_id:
        db.update_task_fields(task_id, email_status="sent", sent_at=datetime.now(timezone.utc))

    logger.info("Sent email to %s (id=%s)", to_email, msg_id)
    return msg_id


def send_reply_to_contact(
    account: dict,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    thread_id: str,
    in_reply_to_message_id: str,
    summary: str = "",
    contact_id: int = None,
    task_id: int = None,
) -> str | None:
    """Send a threaded Gmail reply."""
    creds = _get_credentials(account)
    if not creds:
        logger.error("send_reply_to_contact: could not load credentials for account %d", account["id"])
        return None

    service = build("gmail", "v1", credentials=creds)

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg = MIMEMultipart("alternative")
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = reply_subject
    msg["In-Reply-To"] = in_reply_to_message_id
    msg["References"] = in_reply_to_message_id
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id}
    ).execute()
    msg_id = result.get("id")

    db.log_email(to_name, to_email, reply_subject, summary, msg_id, contact_id, task_id,
                 email_account_id=account["id"])

    if task_id:
        db.update_task_fields(task_id, email_status="sent", sent_at=datetime.now(timezone.utc))

    logger.info("Sent Gmail reply to %s in thread %s", to_email, thread_id)
    return msg_id


def fetch_inbox(account: dict, max_results: int = 50) -> list[dict]:
    """Fetch recent inbox messages for one Gmail account."""
    creds = _get_credentials(account)
    if not creds:
        return []

    service = build("gmail", "v1", credentials=creds)

    results = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
    ).execute()
    messages = results.get("messages", [])

    new_matches = []
    for msg_ref in messages:
        msg_id = msg_ref["id"]
        full_msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"] for h in full_msg.get("payload", {}).get("headers", [])}
        from_raw = headers.get("from", "")
        subject = headers.get("subject", "")
        date_str = headers.get("date", "")

        thread_id = full_msg.get("threadId", "")
        from_email = _extract_email(from_raw)
        received_at = _parse_date(date_str)
        body = _extract_body(full_msg)

        contact = db.get_contact_by_email(from_email) if from_email else None
        contact_id = contact["id"] if contact else None

        if not contact_id:
            continue

        task_id = None
        tasks = db.get_all_tasks()
        for t in tasks:
            if t["contact_id"] == contact_id and t["email_status"] == "sent":
                task_id = t["id"]
                break

        saved_id = db.save_inbox_message(
            msg_id, thread_id, contact_id, task_id,
            from_email, subject, body, received_at, provider="gmail"
        )
        if saved_id is not None:
            new_matches.append({
                "id": saved_id,
                "contact_name": contact["name"] if contact else from_email,
                "subject": subject,
                "body": body,
                "task_id": task_id,
                "from_email": from_email,
            })
            if task_id:
                task = db.get_task(task_id)
                if task and task["status"] == "dm_sent":
                    db.update_task_status(task_id, "replied")

    return new_matches


def _extract_email(from_raw: str) -> str:
    """Extract plain email address from 'Name <email>' format."""
    if "<" in from_raw and ">" in from_raw:
        return from_raw.split("<")[1].split(">")[0].strip().lower()
    return from_raw.strip().lower()


def _parse_date(date_str: str) -> datetime:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return datetime.now(timezone.utc)


def _extract_body(msg: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    payload = msg.get("payload", {})
    return _decode_part(payload)


def _decode_part(part: dict) -> str:
    mime = part.get("mimeType", "")
    if mime == "text/plain":
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for subpart in part.get("parts", []):
        result = _decode_part(subpart)
        if result:
            return result
    return ""
