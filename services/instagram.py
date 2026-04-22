"""Instagram Graph API — OAuth, Reels publishing."""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

import db

logger = logging.getLogger(__name__)

_PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "http://localhost:8000")
FALLBACK_REDIRECT_URI = os.environ.get(
    "INSTAGRAM_REDIRECT_URI", f"{_PORTAL_BASE}/settings/instagram/callback"
)

_AUTH_URL    = "https://www.facebook.com/v19.0/dialog/oauth"
_TOKEN_URL   = "https://graph.facebook.com/v19.0/oauth/access_token"
_LONG_TOKEN_URL = "https://graph.facebook.com/v19.0/oauth/access_token"
_GRAPH_BASE  = "https://graph.facebook.com/v19.0"

SCOPES = ["instagram_basic", "instagram_content_publish", "pages_show_list"]

_POLL_INTERVAL = 10
_MAX_WAIT = 300


# ── Helpers ───────────────────────────────────────────────────────────────────

def _app_id() -> str:
    return db.get_setting("instagram_app_id", os.environ.get("FACEBOOK_APP_ID", ""))


def _app_secret() -> str:
    return db.get_setting("instagram_app_secret", os.environ.get("FACEBOOK_APP_SECRET", ""))


def _redirect_uri() -> str:
    return db.get_setting("instagram_callback_url", "") or FALLBACK_REDIRECT_URI


def _get_valid_token() -> str | None:
    token = db.get_setting("instagram_access_token", "")
    if not token:
        return None
    expiry_str = db.get_setting("instagram_token_expiry", "")
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expiry - timedelta(days=5):
                logger.warning("Instagram token near expiry — user must reconnect")
        except ValueError:
            pass
    return token


def _ig_user_id() -> str:
    return db.get_setting("instagram_user_id", "")


def is_connected() -> bool:
    return bool(db.get_setting("instagram_access_token", ""))


def disconnect():
    for key in ("instagram_access_token", "instagram_token_expiry", "instagram_user_id"):
        db.set_setting(key, "")


# ── OAuth ─────────────────────────────────────────────────────────────────────

def get_auth_url() -> str | None:
    app_id = _app_id()
    if not app_id:
        return None
    params = {
        "client_id": app_id,
        "redirect_uri": _redirect_uri(),
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    req = requests.Request("GET", _AUTH_URL, params=params).prepare()
    return req.url


def exchange_code(code: str) -> bool:
    """Exchange short-lived code for long-lived token and fetch IG user ID."""
    # Step 1: short-lived token
    resp = requests.get(_TOKEN_URL, params={
        "client_id": _app_id(),
        "client_secret": _app_secret(),
        "redirect_uri": _redirect_uri(),
        "code": code,
    })
    if not resp.ok:
        logger.error("Instagram token exchange failed: %s %s", resp.status_code, resp.text)
        return False
    short_token = resp.json().get("access_token", "")

    # Step 2: exchange for long-lived token (~60 days)
    long_resp = requests.get(_LONG_TOKEN_URL, params={
        "grant_type": "fb_exchange_token",
        "client_id": _app_id(),
        "client_secret": _app_secret(),
        "fb_exchange_token": short_token,
    })
    if not long_resp.ok:
        logger.warning("Instagram long-lived token exchange failed, using short-lived: %s", long_resp.text)
        long_token = short_token
        expires_in = 3600
    else:
        data = long_resp.json()
        long_token = data.get("access_token", short_token)
        expires_in = data.get("expires_in", 5184000)  # ~60 days

    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    db.set_setting("instagram_access_token", long_token)
    db.set_setting("instagram_token_expiry", expiry.isoformat())

    # Step 3: fetch the IG professional user ID
    ig_user_id = _fetch_ig_user_id(long_token)
    if ig_user_id:
        db.set_setting("instagram_user_id", ig_user_id)
    return bool(ig_user_id)


def _fetch_ig_user_id(token: str) -> str | None:
    """Resolve the Instagram user ID from the FB user's connected IG accounts."""
    # Get the FB user's pages
    pages_resp = requests.get(
        f"{_GRAPH_BASE}/me/accounts",
        params={"access_token": token},
    )
    if not pages_resp.ok:
        logger.error("Instagram fetch pages failed: %s", pages_resp.text)
        return None

    pages = pages_resp.json().get("data", [])
    for page in pages:
        page_token = page.get("access_token", token)
        page_id = page["id"]
        ig_resp = requests.get(
            f"{_GRAPH_BASE}/{page_id}",
            params={"fields": "instagram_business_account", "access_token": page_token},
        )
        if ig_resp.ok:
            ig_account = ig_resp.json().get("instagram_business_account", {})
            if ig_account.get("id"):
                logger.info("Found Instagram user ID: %s", ig_account["id"])
                return ig_account["id"]

    # Fallback: try /me directly for Creator accounts
    me_resp = requests.get(
        f"{_GRAPH_BASE}/me",
        params={"fields": "id", "access_token": token},
    )
    if me_resp.ok:
        return me_resp.json().get("id")
    return None


# ── Publishing ────────────────────────────────────────────────────────────────

def publish_reel(video_url: str, caption: str) -> str | None:
    """
    Publish a Reel to Instagram.
    video_url must be a publicly accessible URL.
    Returns the Instagram post ID or None on failure.
    """
    token = _get_valid_token()
    ig_user_id = _ig_user_id()
    if not token or not ig_user_id:
        logger.error("Instagram: not connected (no token or user ID)")
        return None

    # Step 1: Create media container
    container_resp = requests.post(
        f"{_GRAPH_BASE}/{ig_user_id}/media",
        params={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
    )
    if not container_resp.ok:
        logger.error("Instagram media container failed: %s %s", container_resp.status_code, container_resp.text)
        return None

    container_id = container_resp.json().get("id")
    logger.info("Instagram media container created: %s", container_id)

    # Step 2: Poll until container is ready
    waited = 0
    while waited < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        status_resp = requests.get(
            f"{_GRAPH_BASE}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
        )
        if not status_resp.ok:
            continue
        status = status_resp.json()
        code = status.get("status_code", "")
        logger.info("Instagram container status: %s (%ds elapsed)", code, waited)
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            logger.error("Instagram container failed with status: %s", status)
            return None

    # Step 3: Publish
    publish_resp = requests.post(
        f"{_GRAPH_BASE}/{ig_user_id}/media_publish",
        params={"creation_id": container_id, "access_token": token},
    )
    if not publish_resp.ok:
        logger.error("Instagram publish failed: %s %s", publish_resp.status_code, publish_resp.text)
        return None

    post_id = publish_resp.json().get("id")
    logger.info("Published Reel to Instagram: %s", post_id)
    return post_id
