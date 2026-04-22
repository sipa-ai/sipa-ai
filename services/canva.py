"""Canva Connect API — OAuth, template listing, asset upload, autofill, export."""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

import db

logger = logging.getLogger(__name__)

_PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "http://localhost:8000")
FALLBACK_REDIRECT_URI = os.environ.get(
    "CANVA_REDIRECT_URI", f"{_PORTAL_BASE}/settings/canva/callback"
)

_AUTH_URL   = "https://www.canva.com/api/oauth/authorize"
_TOKEN_URL  = "https://api.canva.com/rest/v1/oauth/token"
_API_BASE   = "https://api.canva.com/rest/v1"

SCOPES = [
    "design:content:read",
    "design:content:write",
    "asset:read",
    "asset:write",
]

_POLL_INTERVAL = 5
_MAX_WAIT = 300


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_id() -> str:
    return db.get_setting("canva_client_id", os.environ.get("CANVA_CLIENT_ID", ""))


def _client_secret() -> str:
    return db.get_setting("canva_client_secret", os.environ.get("CANVA_CLIENT_SECRET", ""))


def _redirect_uri() -> str:
    return db.get_setting("canva_callback_url", "") or FALLBACK_REDIRECT_URI


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_valid_token() -> str | None:
    token = db.get_setting("canva_access_token", "")
    if not token:
        return None
    expiry_str = db.get_setting("canva_token_expiry", "")
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expiry - timedelta(minutes=5):
                refreshed = _refresh_token()
                return refreshed
        except ValueError:
            pass
    return token


def _refresh_token() -> str | None:
    refresh = db.get_setting("canva_refresh_token", "")
    if not refresh:
        return None
    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    })
    if not resp.ok:
        logger.error("Canva token refresh failed: %s %s", resp.status_code, resp.text)
        return None
    data = resp.json()
    _save_tokens(data)
    return data["access_token"]


def _save_tokens(data: dict):
    expires_in = data.get("expires_in", 3600)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    db.set_setting("canva_access_token", data["access_token"])
    db.set_setting("canva_token_expiry", expiry.isoformat())
    if data.get("refresh_token"):
        db.set_setting("canva_refresh_token", data["refresh_token"])


# ── OAuth ─────────────────────────────────────────────────────────────────────

def get_auth_url() -> str | None:
    cid = _client_id()
    if not cid:
        return None
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": _redirect_uri(),
        "scope": " ".join(SCOPES),
    }
    req = requests.Request("GET", _AUTH_URL, params=params).prepare()
    return req.url


def exchange_code(code: str) -> bool:
    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    })
    if not resp.ok:
        logger.error("Canva token exchange failed: %s %s", resp.status_code, resp.text)
        return False
    _save_tokens(resp.json())
    return True


def is_connected() -> bool:
    return bool(db.get_setting("canva_access_token", ""))


def disconnect():
    for key in ("canva_access_token", "canva_refresh_token", "canva_token_expiry"):
        db.set_setting(key, "")


# ── Templates ─────────────────────────────────────────────────────────────────

def list_video_templates() -> list[dict]:
    """Return list of {id, name, thumbnail_url} for the user's video designs."""
    token = _get_valid_token()
    if not token:
        return []
    results = []
    continuation = None
    while True:
        params = {"type": "VIDEO", "limit": 50}
        if continuation:
            params["continuation"] = continuation
        resp = requests.get(
            f"{_API_BASE}/designs",
            headers=_auth_headers(token),
            params=params,
        )
        if not resp.ok:
            logger.error("Canva list designs failed: %s %s", resp.status_code, resp.text)
            break
        data = resp.json()
        for d in data.get("items", []):
            thumb = None
            if d.get("thumbnail"):
                thumb = d["thumbnail"].get("url")
            results.append({"id": d["id"], "name": d.get("title", d["id"]), "thumbnail_url": thumb})
        continuation = data.get("continuation")
        if not continuation:
            break
    return results


def get_template_fields(template_id: str) -> list[dict]:
    """Return the autofill data fields defined in a Canva template design."""
    token = _get_valid_token()
    if not token:
        return []
    resp = requests.get(
        f"{_API_BASE}/designs/{template_id}/autofill/fields",
        headers=_auth_headers(token),
    )
    if not resp.ok:
        logger.warning(
            "Canva get template fields failed: %s %s — falling back to design metadata",
            resp.status_code, resp.text,
        )
        return _get_fields_from_design(token, template_id)
    data = resp.json()
    fields = []
    for f in data.get("fields", []):
        fields.append({
            "name": f.get("name") or f.get("id", ""),
            "type": f.get("type", "text"),
        })
    return fields


def _get_fields_from_design(token: str, template_id: str) -> list[dict]:
    resp = requests.get(
        f"{_API_BASE}/designs/{template_id}",
        headers=_auth_headers(token),
    )
    if not resp.ok:
        return []
    data = resp.json().get("design", {})
    fields = []
    for page in data.get("pages", []):
        for element in page.get("elements", []):
            if element.get("type") in ("TEXT", "IMAGE"):
                name = element.get("name") or element.get("id", "")
                fields.append({"name": name, "type": element["type"].lower()})
    return fields


# ── Asset upload ──────────────────────────────────────────────────────────────

def upload_asset(image_bytes: bytes, mime_type: str, name: str = "background") -> str | None:
    """Upload an image as a Canva asset. Returns the asset ID or None."""
    token = _get_valid_token()
    if not token:
        return None
    import base64
    # Canva asset upload: multipart/form-data
    resp = requests.post(
        f"{_API_BASE}/assets",
        headers={"Authorization": f"Bearer {token}"},
        files={"asset_content": (f"{name}.jpg", image_bytes, mime_type)},
        data={"name_base64": base64.b64encode(name.encode()).decode()},
    )
    if not resp.ok:
        logger.error("Canva asset upload failed: %s %s", resp.status_code, resp.text)
        return None
    job = resp.json().get("job", {})
    asset_id = job.get("id")
    # Poll until asset is ready
    waited = 0
    while waited < _MAX_WAIT:
        status_resp = requests.get(
            f"{_API_BASE}/assets/{asset_id}",
            headers=_auth_headers(token),
        )
        if status_resp.ok:
            asset = status_resp.json().get("asset", {})
            if asset.get("status") == "success":
                return asset_id
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    logger.error("Canva asset upload timed out for asset %s", asset_id)
    return None


# ── Autofill ──────────────────────────────────────────────────────────────────

def autofill(template_id: str, fields: dict) -> str | None:
    """
    Autofill a Canva template and return the resulting design ID.
    fields = {canva_field_name: value_or_asset_id}
    Text values are strings; image values are Canva asset IDs.
    Mappings stored in DB tell us which field is text and which is image.
    """
    token = _get_valid_token()
    if not token:
        return None

    mappings = {m["canva_field_name"]: m["mapped_to"] for m in db.get_canva_field_mappings(template_id)}
    data_fields = []
    for field_name, value in fields.items():
        field_type = mappings.get(field_name, "theme")
        if field_type == "image":
            data_fields.append({
                "name": field_name,
                "type": "image",
                "asset_id": value,
            })
        else:
            data_fields.append({
                "name": field_name,
                "type": "text",
                "text": str(value),
            })

    resp = requests.post(
        f"{_API_BASE}/designs/{template_id}/autofill",
        headers=_auth_headers(token),
        json={"data": data_fields},
    )
    if not resp.ok:
        logger.error("Canva autofill failed: %s %s", resp.status_code, resp.text)
        return None

    job = resp.json().get("job", {})
    job_id = job.get("id")

    # Poll until the autofill job is done
    waited = 0
    while waited < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        poll = requests.get(
            f"{_API_BASE}/designs/{template_id}/autofill/{job_id}",
            headers=_auth_headers(token),
        )
        if not poll.ok:
            continue
        result = poll.json().get("job", {})
        if result.get("status") == "success":
            return result.get("result", {}).get("design", {}).get("id")
        if result.get("status") == "failed":
            logger.error("Canva autofill job failed: %s", result)
            return None

    logger.error("Canva autofill timed out for template %s", template_id)
    return None


# ── Export ────────────────────────────────────────────────────────────────────

def export_video(design_id: str) -> bytes | None:
    """Export a Canva design as MP4. Returns the video bytes or None."""
    token = _get_valid_token()
    if not token:
        return None

    resp = requests.post(
        f"{_API_BASE}/exports",
        headers=_auth_headers(token),
        json={"design_id": design_id, "format": "mp4"},
    )
    if not resp.ok:
        logger.error("Canva export failed: %s %s", resp.status_code, resp.text)
        return None

    job = resp.json().get("job", {})
    job_id = job.get("id")

    waited = 0
    while waited < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        poll = requests.get(
            f"{_API_BASE}/exports/{job_id}",
            headers=_auth_headers(token),
        )
        if not poll.ok:
            continue
        result = poll.json().get("job", {})
        if result.get("status") == "success":
            urls = result.get("urls", [])
            if not urls:
                logger.error("Canva export succeeded but returned no URLs")
                return None
            video_resp = requests.get(urls[0])
            if not video_resp.ok:
                logger.error("Canva export download failed: %s", video_resp.status_code)
                return None
            logger.info("Exported Canva design %s to MP4 (%d bytes)", design_id, len(video_resp.content))
            return video_resp.content
        if result.get("status") == "failed":
            logger.error("Canva export job failed: %s", result)
            return None

    logger.error("Canva export timed out for design %s", design_id)
    return None
