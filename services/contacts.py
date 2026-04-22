"""Task and post serialisation helpers."""

import json


def tasks_to_json() -> str:
    """Return all tasks as a JSON string."""
    import db

    return json.dumps([dict(t) for t in db.get_all_tasks()], default=str)


def posts_to_json(posts=None) -> str:
    """Return posts as JSON, excluding binary image data.

    If posts is None, fetches all posts from the database.
    """
    import db

    if posts is None:
        posts = db.get_all_posts()
    return json.dumps(
        [
            {
                k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                for k, v in dict(p).items()
                if k != "image_bytes"
            }
            for p in posts
        ],
        default=str,
    )
