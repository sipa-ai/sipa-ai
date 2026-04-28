"""Outlook 365 integration — MSAL delegated OAuth, Microsoft Graph API."""

import logging
import os
from datetime import datetime, timezone, timedelta

import msal
import requests

import db

logger = logging.getLogger(__name__)

SCOPES = ["Mail.Send", "Mail.ReadWrite", "offline_access"]

_PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "http://localhost:8000")
FALLBACK_REDIRECT_URI = os.environ.get(
    "OUTLOOK_CALLBACK_URL", f"{_PORTAL_BASE}/settings/email/callback/outlook"
)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _redirect_uri(account: dict) -> str:
    return (account.get("callback_url") or "").strip() or FALLBACK_REDIRECT_URI


def _authority(account: dict) -> str:
    tenant = (account.get("tenant_id") or "organizations").strip()
    return f"https://login.microsoftonline.com/{tenant}"


def _msal_app(account: dict) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        account["client_id"],
        client_credential=account["client_secret"],
        authority=_authority(account),
    )


def get_auth_url(account_id: int) -> str | None:
    account = db.get_email_account(account_id)
    if not account or not account["client_id"] or not account["client_secret"]:
        return None
    app = _msal_app(account)
    return app.get_authorization_request_url(
        scopes=SCOPES,
        redirect_uri=_redirect_uri(account),
        state=str(account_id),
    )


def exchange_code(code: str, account_id: int) -> bool:
    account = db.get_email_account(account_id)
    if not account or not account["client_id"] or not account["client_secret"]:
        return False
    app = _msal_app(account)
    result = app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=_redirect_uri(account),
    )
    if "access_token" not in result:
        logger.error("Outlook exchange_code failed: %s", result.get("error_description"))
        return False

    expiry = datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))

    # Fetch the signed-in user's email via Graph
    headers = {"Authorization": f"Bearer {result['access_token']}"}
    me = requests.get(f"{GRAPH_BASE}/me", headers=headers, timeout=10).json()
    email = me.get("mail") or me.get("userPrincipalName") or ""

    db.save_email_tokens(
        account_id, email,
        result["access_token"],
        result.get("refresh_token", ""),
        expiry,
    )
    return True


def _get_access_token(account: dict) -> str | None:
    """Return a valid access token, refreshing via MSAL if expired."""
    expiry = account.get("token_expiry")
    if expiry:
        if hasattr(expiry, "tzinfo") and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        # Still valid with 5-minute buffer
        if datetime.now(timezone.utc) < expiry - timedelta(minutes=5):
            return account["access_token"]

    if not account.get("refresh_token"):
        logger.error("Outlook: no refresh token for account %d", account["id"])
        return None

    app = _msal_app(account)
    result = app.acquire_token_by_refresh_token(account["refresh_token"], scopes=SCOPES)
    if "access_token" not in result:
        logger.error("Outlook token refresh failed: %s", result.get("error_description"))
        return None

    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))
    db.update_email_access_token(account["id"], result["access_token"], new_expiry)
    return result["access_token"]


def send_email_to_contact(
    account: dict,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    summary: str = "",
    contact_id: int = None,
    task_id: int = None,
    cc: str = "",
) -> str | None:
    """Send an email via Microsoft Graph. Returns the Graph message ID or None on error."""
    token = _get_access_token(account)
    if not token:
        return None

    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [
            {"emailAddress": {"address": to_email, "name": to_name or to_email}}
        ],
    }
    if cc:
        message["ccRecipients"] = [
            {"emailAddress": {"address": addr.strip()}}
            for addr in cc.split(",") if addr.strip()
        ]
    payload = {"message": message, "saveToSentItems": True}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(f"{GRAPH_BASE}/me/sendMail", json=payload, headers=headers, timeout=30)

    if resp.status_code not in (200, 202):
        logger.error("Outlook sendMail failed: %s %s", resp.status_code, resp.text)
        return None

    # Graph sendMail returns 202 with no body — use a synthetic ID for logging
    msg_id = f"outlook:{account['id']}:{datetime.now(timezone.utc).timestamp()}"

    db.log_email(to_name, to_email, subject, summary, msg_id, contact_id, task_id,
                 email_account_id=account["id"])

    if task_id:
        db.update_task_fields(task_id, email_status="sent", sent_at=datetime.now(timezone.utc))

    logger.info("Sent Outlook email to %s (account %d)", to_email, account["id"])
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
    cc: str = "",
) -> str | None:
    """Send a threaded Outlook reply using the original Graph message ID."""
    token = _get_access_token(account)
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Strip any prefix we added for logging synthetic IDs
    graph_msg_id = in_reply_to_message_id
    if graph_msg_id.startswith("outlook:"):
        # Can't thread reply to a synthetic ID — fall back to new message
        logger.warning("Outlook: synthetic message ID, sending as new email instead of reply")
        return send_email_to_contact(account, to_email, to_name, subject, body,
                                     summary, contact_id, task_id, cc)

    reply_message: dict = {"body": {"contentType": "Text", "content": body}}
    if cc:
        reply_message["ccRecipients"] = [
            {"emailAddress": {"address": addr.strip()}}
            for addr in cc.split(",") if addr.strip()
        ]
    reply_payload = {
        "message": reply_message,
        "comment": body,
    }
    resp = requests.post(
        f"{GRAPH_BASE}/me/messages/{graph_msg_id}/reply",
        json=reply_payload,
        headers=headers,
        timeout=30,
    )

    if resp.status_code not in (200, 202):
        logger.error("Outlook reply failed: %s %s", resp.status_code, resp.text)
        return None

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg_id = f"outlook:{account['id']}:{datetime.now(timezone.utc).timestamp()}"

    db.log_email(to_name, to_email, reply_subject, summary, msg_id, contact_id, task_id,
                 email_account_id=account["id"])

    if task_id:
        db.update_task_fields(task_id, email_status="sent", sent_at=datetime.now(timezone.utc))

    logger.info("Sent Outlook reply to %s", to_email)
    return msg_id


def fetch_inbox(account: dict, max_results: int = 50) -> list[dict]:
    """Fetch recent inbox messages for one Outlook account."""
    token = _get_access_token(account)
    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "$top": max_results,
        "$select": "id,subject,from,receivedDateTime,body,conversationId,internetMessageId",
        "$orderby": "receivedDateTime desc",
    }
    resp = requests.get(
        f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
        headers=headers,
        params=params,
        timeout=30,
    )
    if resp.status_code != 200:
        logger.error("Outlook fetch_inbox failed: %s %s", resp.status_code, resp.text)
        return []

    messages = resp.json().get("value", [])
    new_matches = []

    for msg in messages:
        msg_id = msg["id"]
        from_addr = msg.get("from", {}).get("emailAddress", {})
        from_email = from_addr.get("address", "").lower()
        subject = msg.get("subject", "")
        body = msg.get("body", {}).get("content", "")
        conversation_id = msg.get("conversationId", "")
        received_at_str = msg.get("receivedDateTime", "")

        try:
            received_at = datetime.fromisoformat(received_at_str.replace("Z", "+00:00"))
        except Exception:
            received_at = datetime.now(timezone.utc)

        contact = db.get_contact_by_email(from_email) if from_email else None
        contact_id = contact["id"] if contact else None

        if not contact_id:
            continue

        task_id = None
        for t in db.get_all_tasks():
            if t["contact_id"] == contact_id and t["email_status"] == "sent":
                task_id = t["id"]
                break

        saved_id = db.save_inbox_message(
            msg_id, conversation_id, contact_id, task_id,
            from_email, subject, body, received_at, provider="outlook"
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
