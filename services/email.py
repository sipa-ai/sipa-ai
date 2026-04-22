"""Email dispatcher — routes send/receive to the right provider (Gmail or Outlook)."""

import logging

import db

logger = logging.getLogger(__name__)


def send_email_to_contact(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    summary: str = "",
    contact_id: int = None,
    task_id: int = None,
) -> str | None:
    """Send an email, routing to Gmail or Outlook based on account resolution."""
    account = db.get_account_for_contact(contact_id)
    if not account:
        logger.error("send_email_to_contact: no connected email account")
        return None
    provider = account.get("provider", "gmail")
    if provider == "gmail":
        from services.gmail import send_email_to_contact as _send
    elif provider == "outlook":
        from services.outlook import send_email_to_contact as _send
    else:
        logger.error("Unknown email provider: %s", provider)
        return None
    return _send(account, to_email, to_name, subject, body, summary, contact_id, task_id)


def send_reply_to_contact(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    thread_id: str,
    in_reply_to_message_id: str,
    inbox_provider: str = "gmail",
    summary: str = "",
    contact_id: int = None,
    task_id: int = None,
) -> str | None:
    """Send a threaded reply, routing to the provider that received the original."""
    # Use the provider that received the original email to keep the thread consistent
    accounts = db.get_connected_email_accounts()
    account = None
    for acc in accounts:
        if acc.get("provider") == inbox_provider:
            account = acc
            break
    if not account:
        # Fall back to contact preference / default
        account = db.get_account_for_contact(contact_id)
    if not account:
        logger.error("send_reply_to_contact: no connected email account")
        return None

    provider = account.get("provider", "gmail")
    if provider == "gmail":
        from services.gmail import send_reply_to_contact as _send
    elif provider == "outlook":
        from services.outlook import send_reply_to_contact as _send
    else:
        logger.error("Unknown email provider: %s", provider)
        return None
    return _send(account, to_email, to_name, subject, body, thread_id,
                 in_reply_to_message_id, summary, contact_id, task_id)


def fetch_all_inboxes(max_results: int = 50) -> list[dict]:
    """Poll all connected accounts (Gmail + Outlook) and return new matched messages."""
    all_matches = []
    for account in db.get_connected_email_accounts():
        provider = account.get("provider", "gmail")
        try:
            if provider == "gmail":
                from services.gmail import fetch_inbox
            elif provider == "outlook":
                from services.outlook import fetch_inbox
            else:
                continue
            matches = fetch_inbox(account, max_results)
            all_matches.extend(matches)
        except Exception as e:
            logger.error("fetch_inbox failed for account %d (%s, %s): %s",
                         account["id"], provider, account.get("email", "?"), e)
    return all_matches
