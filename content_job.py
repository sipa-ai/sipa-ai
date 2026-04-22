"""Content delivery job — sends scheduled, pre-approved posts from the database."""

import logging
from datetime import date

from telegram import Bot

import db
from services.media import send_post_media

logger = logging.getLogger(__name__)


def _allowed_user_ids() -> list[int]:
    raw = db.get_setting("allowed_user_ids", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


async def job_content(bot: Bot) -> None:
    today = date.today().strftime("%Y-%m-%d")
    post = db.get_post_for_date(today)

    if not post:
        logger.info("No approved content scheduled for %s", today)
        return

    post_id = post["id"]
    logger.info("Sending content for %s — %s", today, post["theme"])

    try:
        if post["format"] in ("static", "carousel"):
            caption = f"Caption — {today}\n\n{post['caption'] or ''}"
            for user_id in _allowed_user_ids():
                await send_post_media(dict(post), user_id, bot, caption)

        db.mark_post_sent(post_id)
        logger.info("Content sent for %s", today)

    except ValueError as e:
        logger.error("Post %s has no media — skipping: %s", post_id, e)
    except Exception as e:
        logger.error("Content job failed for %s: %s", today, e)
        for user_id in _allowed_user_ids():
            await bot.send_message(chat_id=user_id, text=f"Content delivery failed for {today}:\n{e}")
