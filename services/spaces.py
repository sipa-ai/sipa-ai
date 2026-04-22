"""DigitalOcean Spaces — temporary video hosting for Instagram publishing."""

import logging
import os
import uuid

import boto3
from botocore.client import Config

logger = logging.getLogger(__name__)

_ENDPOINT = os.environ.get("DO_SPACES_ENDPOINT", "")
_REGION   = os.environ.get("DO_SPACES_REGION", "nyc3")
_BUCKET   = os.environ.get("DO_SPACES_BUCKET", "")
_KEY      = os.environ.get("DO_SPACES_KEY", "")
_SECRET   = os.environ.get("DO_SPACES_SECRET", "")


def _client():
    return boto3.client(
        "s3",
        region_name=_REGION,
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_KEY,
        aws_secret_access_key=_SECRET,
        config=Config(signature_version="s3v4"),
    )


def upload_video(video_bytes: bytes, key: str | None = None) -> str:
    """Upload MP4 bytes to Spaces. Returns the public URL."""
    if not key:
        key = f"canva_video_tmp/{uuid.uuid4().hex}.mp4"
    client = _client()
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=video_bytes,
        ContentType="video/mp4",
        ACL="public-read",
    )
    url = f"{_ENDPOINT}/{_BUCKET}/{key}"
    logger.info("Uploaded video to Spaces: %s", url)
    return url, key


def delete_video(key: str):
    """Delete a video from Spaces by key."""
    try:
        _client().delete_object(Bucket=_BUCKET, Key=key)
        logger.info("Deleted video from Spaces: %s", key)
    except Exception as exc:
        logger.warning("Failed to delete Spaces object %s: %s", key, exc)
