"""Media generation and delivery — images, videos, and Telegram sending."""

import asyncio
import logging
import os
from io import BytesIO

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# ── Image generation ──────────────────────────────────────────────────────────

_REALISTIC_MODEL = os.environ.get("IMAGEN_REALISTIC_MODEL", "imagen-4.0-generate-001")


def _get_client() -> genai.Client:
    import db
    key = db.get_setting("google_ai_studio_key", "")
    if not key:
        raise ValueError("Google AI Studio key not configured. Set it in portal Settings.")
    return genai.Client(api_key=key)


def _get_artistic_models() -> list[str]:
    import db
    raw = db.get_setting(
        "gemini_image_model",
        "gemini-3.1-flash-image-preview,gemini-2.5-flash-image",
    )
    return [m.strip() for m in raw.split(",") if m.strip()]


def _generate_imagen_sync(prompt: str, model: str) -> tuple[bytes, str]:
    response = _get_client().models.generate_images(
        model=model,
        prompt=prompt,
        config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="1:1"),
    )
    if response.generated_images:
        img = response.generated_images[0].image
        return img.image_bytes, "image/jpeg"
    raise ValueError(f"No image returned by {model}")


def _generate_gemini_sync(prompt: str, model: str) -> tuple[bytes, str]:
    response = _get_client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"], temperature=1.0),
    )
    candidates = response.candidates or []
    for candidate in candidates:
        parts = (candidate.content and candidate.content.parts) or []
        for part in parts:
            if part.inline_data is not None:
                return part.inline_data.data, part.inline_data.mime_type or "image/jpeg"
    raise ValueError(
        f"No image returned by {model}. "
        f"finish_reason={candidates[0].finish_reason if candidates else 'no candidates'}"
    )


def _try_generate_sync(prompt: str, model: str) -> tuple[bytes, str]:
    if model.startswith("imagen"):
        return _generate_imagen_sync(prompt, model)
    return _generate_gemini_sync(prompt, model)


async def generate_image(prompt: str, style_type: str = "realistic") -> tuple[bytes, str, str]:
    """Generate an image. Returns (image_bytes, mime_type, model_used).

    style_type='realistic' → Imagen 4 (photographic quality)
    style_type='artistic'  → Gemini flash chain (illustrated/creative)
    """
    if style_type == "realistic":
        models = [(_REALISTIC_MODEL, 3)]
    else:
        artistic = _get_artistic_models()
        first, *rest = artistic
        models = [(first, 5)] + [(m, 3) for m in rest]

    last_exc: Exception = RuntimeError("No image models configured")
    for model, max_attempts in models:
        for attempt in range(1, max_attempts + 1):
            try:
                img_bytes, mime = await asyncio.to_thread(_try_generate_sync, prompt, model)
                return img_bytes, mime, model
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts:
                    delay = 2 ** attempt
                    logger.warning(
                        "Image gen failed (model=%s attempt=%d/%d), retry in %ds: %s",
                        model, attempt, max_attempts, delay, exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "Image gen failed (model=%s attempt=%d/%d), next model: %s",
                        model, attempt, max_attempts, exc,
                    )
    raise last_exc


# ── Video generation ──────────────────────────────────────────────────────────

_VEO_MODEL = os.environ.get("VEO_MODEL", "veo-3.1-generate-preview")

_POLL_INTERVAL = 10
_MAX_WAIT = 300


def _generate_video_sync(prompt: str) -> tuple[bytes, str]:
    import time

    client = _get_client()
    operation = client.models.generate_videos(
        model=_VEO_MODEL,
        prompt=prompt,
        config=types.GenerateVideosConfig(aspect_ratio="9:16", duration_seconds=8),
    )
    waited = 0
    while not operation.done:
        if waited >= _MAX_WAIT:
            raise TimeoutError(f"Veo generation timed out after {_MAX_WAIT}s")
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        operation = client.operations.get(operation)
        logger.info("Veo generation in progress (%ds elapsed)…", waited)

    videos = operation.response.generated_videos
    if not videos:
        raise ValueError("Veo returned no videos")

    video_data = client.files.download(file=videos[0].video)
    return bytes(video_data), "video/mp4"


async def generate_video(prompt: str) -> tuple[bytes, str]:
    """Async wrapper around Veo generation."""
    return await asyncio.to_thread(_generate_video_sync, prompt)


# ── Telegram media delivery ───────────────────────────────────────────────────

async def send_post_media(post: dict, chat_id: int, bot, caption: str) -> None:
    """Send a post's image(s) to one Telegram chat.

    post must include image_bytes (use db.get_post or db.get_post_for_date).
    Raises ValueError if the required media is missing.
    """
    from telegram import InputMediaPhoto
    import db

    if post["format"] == "static":
        if not post.get("image_bytes"):
            raise ValueError(f"No image for post {post['id']} ({post['date']})")
        await bot.send_photo(
            chat_id=chat_id,
            photo=BytesIO(bytes(post["image_bytes"])),
            caption=caption,
        )
    else:
        slides = db.get_slides_with_images(post["id"])
        media = [
            InputMediaPhoto(media=BytesIO(bytes(s["image_bytes"])))
            for s in slides
            if s["image_bytes"]
        ]
        if not media:
            raise ValueError(f"No slide images for post {post['id']} ({post['date']})")
        await bot.send_media_group(chat_id=chat_id, media=media)
        await bot.send_message(chat_id=chat_id, text=caption)
