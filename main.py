"""Entry point — runs the Telegram bot and admin portal in a single process."""

import asyncio
import logging
import os

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CRON_DAILY   = os.environ.get("CRON_DAILY",   "0 7 * * *")
CRON_WEEKLY  = os.environ.get("CRON_WEEKLY",  "0 6 * * 1")
CRON_CONTENT = os.environ.get("CRON_CONTENT", "0 6 * * *")
CRON_GMAIL   = os.environ.get("CRON_GMAIL",   "*/15 * * * *")


async def run():
    import db
    from bot import create_application, job_daily, job_weekly, job_gmail
    from content_job import job_content
    from portal import app as portal_app

    # Initialise database once for both services
    db.init_db()
    logger.info("Database initialised")

    # Build Telegram application (handlers registered, no hooks)
    tg_app = create_application()

    # Start APScheduler inside the running event loop
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_daily,   CronTrigger.from_crontab(CRON_DAILY,   timezone="UTC"), args=[tg_app.bot])
    scheduler.add_job(job_weekly,  CronTrigger.from_crontab(CRON_WEEKLY,  timezone="UTC"), args=[tg_app.bot])
    scheduler.add_job(job_content, CronTrigger.from_crontab(CRON_CONTENT, timezone="UTC"), args=[tg_app.bot])
    scheduler.add_job(job_gmail,   CronTrigger.from_crontab(CRON_GMAIL,   timezone="UTC"), args=[tg_app.bot])
    scheduler.start()
    logger.info(
        "Scheduler started — daily: %s  weekly: %s  content: %s  gmail: %s (UTC)",
        CRON_DAILY, CRON_WEEKLY, CRON_CONTENT, CRON_GMAIL,
    )

    # Configure uvicorn to serve the portal on port 8080
    config = uvicorn.Config(portal_app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)

    # Run Telegram polling and uvicorn concurrently in the same event loop
    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started")

        await server.serve()  # blocks until SIGTERM / SIGINT

        logger.info("Shutting down Telegram bot...")
        await tg_app.updater.stop()
        await tg_app.stop()

    scheduler.shutdown()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(run())
