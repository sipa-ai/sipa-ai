"""LinkedIn integration — OAuth2, image upload, post creation (REST Posts API)."""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

import db

logger = logging.getLogger(__name__)

_PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "http://localhost:8000")
FALLBACK_REDIRECT_URI = os.environ.get(
    "LINKEDIN_CALLBACK_URL", f"{_PORTAL_BASE}/settings/linkedin/callback"
)

_AUTH_URL   = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL  = "https://www.linkedin.com/oauth/v2/accessToken"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_POSTS_URL  = "https://api.linkedin.com/rest/posts"
_IMAGES_URL = "https://api.linkedin.com/rest/images?action=initializeUpload"
_LI_VERSION = "202406"

SCOPES = ["openid", "profile", "email", "w_member_social"]


def _redirect_uri(account: dict) -> str:
    return (account.get("callback_url") or "").strip() or FALLBACK_REDIRECT_URI


def _rest_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "LinkedIn-Version": _LI_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }


# ── OAuth ─────────────────────────────────────────────────────────────────────

def get_auth_url(account_id: int) -> str | None:
    account = db.get_linkedin_account(account_id)
    if not account or not account["client_id"] or not account["client_secret"]:
        return None
    params = {
        "response_type": "code",
        "client_id": account["client_id"],
        "redirect_uri": _redirect_uri(account),
        "scope": " ".join(SCOPES),
        "state": str(account_id),
    }
    req = requests.Request("GET", _AUTH_URL, params=params).prepare()
    return req.url


def exchange_code(code: str, account_id: int) -> bool:
    account = db.get_linkedin_account(account_id)
    if not account or not account["client_id"] or not account["client_secret"]:
        return False
    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(account),
        "client_id": account["client_id"],
        "client_secret": account["client_secret"],
    })
    if not resp.ok:
        logger.error("LinkedIn token exchange failed: %s %s", resp.status_code, resp.text)
        return False
    data = resp.json()
    access_token = data["access_token"]
    # LinkedIn tokens expire in expires_in seconds; refresh tokens are optional
    expires_in = data.get("expires_in", 5183999)  # ~60 days default
    token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    refresh_token = data.get("refresh_token")

    # Fetch user info via OpenID Connect
    ui_resp = requests.get(
        _USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not ui_resp.ok:
        logger.error("LinkedIn userinfo failed: %s", ui_resp.text)
        return False
    ui = ui_resp.json()
    linkedin_id = ui.get("sub", "")
    person_name = ui.get("name", "")

    db.save_linkedin_tokens(account_id, access_token, refresh_token,
                            token_expiry, linkedin_id, person_name)
    return True


def _get_valid_token(account_id: int) -> str | None:
    """Return a valid access token, refreshing if needed."""
    account = db.get_linkedin_account(account_id)
    if not account or not account["access_token"]:
        return None

    expiry = account.get("token_expiry")
    if expiry:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expiry - timedelta(minutes=5):
            # LinkedIn uses long-lived tokens; if we have a refresh token, try it
            if account.get("refresh_token"):
                refreshed = _refresh_token(account)
                if refreshed:
                    return refreshed
            logger.warning("LinkedIn token expired for account %d", account_id)
            return None

    return account["access_token"]


def _refresh_token(account: dict) -> str | None:
    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": account["refresh_token"],
        "client_id": account["client_id"],
        "client_secret": account["client_secret"],
    })
    if not resp.ok:
        logger.error("LinkedIn token refresh failed: %s", resp.text)
        return None
    data = resp.json()
    access_token = data["access_token"]
    expires_in = data.get("expires_in", 5183999)
    token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    db.update_linkedin_access_token(account["id"], access_token, token_expiry)
    return access_token


# ── Image upload ──────────────────────────────────────────────────────────────

def upload_image(account_id: int, image_bytes: bytes, mime_type: str) -> str | None:
    """Upload image bytes to LinkedIn. Returns image URN or None on failure."""
    token = _get_valid_token(account_id)
    if not token:
        return None
    account = db.get_linkedin_account(account_id)
    owner_urn = f"urn:li:person:{account['linkedin_id']}"

    headers = _rest_headers(token)
    init_resp = requests.post(
        _IMAGES_URL,
        headers=headers,
        json={"initializeUploadRequest": {"owner": owner_urn}},
    )
    if not init_resp.ok:
        logger.error("LinkedIn image init failed: %s %s", init_resp.status_code, init_resp.text)
        return None

    value = init_resp.json()["value"]
    upload_url = value["uploadUrl"]
    image_urn = value["image"]

    put_resp = requests.put(
        upload_url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": mime_type},
        data=image_bytes,
    )
    if not put_resp.ok:
        logger.error("LinkedIn image upload failed: %s %s", put_resp.status_code, put_resp.text)
        return None

    logger.info("Uploaded image to LinkedIn: %s", image_urn)
    return image_urn


# ── Post creation ─────────────────────────────────────────────────────────────

def create_post(account_id: int, text: str,
                image_bytes: bytes = None, mime_type: str = None) -> str | None:
    """Create a LinkedIn post. Returns the post URN/ID or None on failure."""
    token = _get_valid_token(account_id)
    if not token:
        logger.error("LinkedIn: no valid token for account %d", account_id)
        return None
    account = db.get_linkedin_account(account_id)
    author_urn = f"urn:li:person:{account['linkedin_id']}"

    image_urn = None
    if image_bytes:
        image_urn = upload_image(account_id, image_bytes, mime_type or "image/jpeg")

    body = {
        "author": author_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        body["content"] = {"media": {"id": image_urn}}

    headers = _rest_headers(token)
    resp = requests.post(_POSTS_URL, headers=headers, json=body)
    if not resp.ok:
        logger.error("LinkedIn post failed: %s %s", resp.status_code, resp.text)
        return None

    post_id = resp.headers.get("x-restli-id", "")
    logger.info("Posted to LinkedIn: %s", post_id)
    return post_id


def _first_connected_account_id() -> int | None:
    accounts = db.get_connected_linkedin_accounts()
    return accounts[0]["id"] if accounts else None
