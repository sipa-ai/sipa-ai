"""Post content generation — captions, slide plans, reel concepts, and full post orchestration."""

import json
import logging

from services.llm import build_brand_prompt, call_claude

logger = logging.getLogger(__name__)


# ── Caption & slide generation ────────────────────────────────────────────────

async def generate_caption(entry: dict, slides: list[dict] | None = None) -> str:
    slide_info = ""
    if slides:
        slide_info = "\n\nSlide titles:\n" + "\n".join(
            f"- Slide {s['slide_number']}: {s['title_en']}" for s in slides
        )
    prompt = (
        f"Today is {entry['date']}. Write the Instagram caption for this post.\n\n"
        f"Format: {entry['format']}\n"
        f"Pillar: {entry['pillar']}\n"
        f"Theme: {entry['theme']}\n"
        f"{slide_info}\n\n"
        "Follow the brand voice and tone from the brand context. "
        "Warm and inviting. Never salesy. Include relevant hashtags."
    )
    response = await call_claude(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=build_brand_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def generate_linkedin_caption(entry: dict) -> str:
    """Generate a LinkedIn-optimised post caption (professional, English-first, no Arabic hashtags)."""
    prompt = (
        f"Write a LinkedIn post based on the brand context.\n\n"
        f"Date: {entry['date']}\n"
        f"Format: {entry['format']}\n"
        f"Pillar: {entry['pillar']}\n"
        f"Theme: {entry['theme']}\n\n"
        "Tone: warm, professional — aimed at the brand's target audience.\n"
        "Length: 3–5 short paragraphs. Include 3–5 relevant hashtags at the end.\n"
        "Never use salesy language. Focus on the value and community impact."
    )
    response = await call_claude(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=build_brand_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def generate_linkedin_article(entry: dict) -> dict:
    """Generate a LinkedIn article with title and long-form body. Returns {"title": ..., "body": ...}."""
    prompt = (
        f"Write a LinkedIn article based on the brand context.\n\n"
        f"Date: {entry['date']}\n"
        f"Pillar: {entry['pillar']}\n"
        f"Theme: {entry['theme']}\n\n"
        "Requirements:\n"
        "- Title: compelling, 8–12 words\n"
        "- Body: 400–600 words, structured with 3–4 sections\n"
        "- Tone: thoughtful, aligned with the brand voice\n"
        "- Include a storytelling angle relevant to the brand's mission and audience\n"
        "- End with a clear call to community (not a sales call)\n\n"
        "Return ONLY valid JSON: {\"title\": \"...\", \"body\": \"...\"}\n"
        "No markdown fences, no extra text."
    )
    response = await call_claude(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=build_brand_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except Exception:
        # Fallback: try to extract JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end]) if start >= 0 else {"title": entry["theme"], "body": text}


async def generate_reel_concept(entry: dict) -> str:
    """Generate a video generation prompt for a reel post."""
    prompt = (
        "Write a video generation prompt for this Instagram Reel.\n\n"
        f"Date: {entry['date']}\n"
        f"Pillar: {entry['pillar']}\n"
        f"Theme: {entry['theme']}\n\n"
        "Requirements:\n"
        "- Vertical 9:16 format, 8 seconds, cinematic social-native style\n"
        "- Use the brand's visual identity (colours, mood, setting) from the brand context\n"
        "- No text overlays, no logos in frame\n\n"
        "Return ONLY the video prompt — no explanation, no markdown, no preamble. "
        "Start directly with the scene description."
    )
    response = await call_claude(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=build_brand_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def generate_slide_plan(entry: dict) -> list[dict]:
    n = entry.get("n_slides", 5)
    prompt = (
        f"Plan exactly {n} slides for this carousel Instagram post.\n\n"
        f"Theme: {entry['theme']}\n"
        f"Pillar: {entry['pillar']}\n\n"
        f"Return ONLY a JSON array of {n} objects with:\n"
        '- "number": integer\n'
        '- "title_en": short English title\n'
        '- "content": one sentence describing the slide\n\n'
        "No markdown, no explanation, only raw JSON."
    )
    response = await call_claude(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=build_brand_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text[text.index("["):]
        text = text[: text.rindex("]") + 1]
    return json.loads(text)


# ── Full post generation orchestration ───────────────────────────────────────

async def generate_for_post(post: dict) -> None:
    """Generate all missing assets for a post (image, slides, caption)."""
    import db
    from services.media import generate_image

    post_id = post["id"]
    style_type = post.get("image_style_type") or "realistic"
    channels = [c.strip() for c in (post.get("channels") or "instagram").split(",")]

    # ── Image generation ──────────────────────────────────────────────────────
    if post["format"] == "static":
        if not post.get("has_image") and not post.get("image_bytes") and post.get("image_prompt"):
            img_bytes, mime, model_used = await generate_image(post["image_prompt"], style_type)
            db.set_post_image(post_id, img_bytes, mime, model_used, post["image_prompt"])
            logger.info("Generated image for post %s (%s) via %s", post_id, post["date"], model_used)

    elif post["format"] == "carousel":
        slides = db.get_slides_for_post(post_id)
        if not slides and post.get("n_slides"):
            slide_plan = await generate_slide_plan(post)
            for s in slide_plan:
                db.upsert_slide(post_id, s)
            slides = db.get_slides_for_post(post_id)

        style_desc = post.get("image_style") or post.get("image_prompt") or ""
        for slide in slides:
            if not slide["has_image"] and slide.get("title_en"):
                prompt = (
                    f"Slide {slide['slide_number']} of {post['n_slides']}: "
                    f"{slide['title_en']} — {slide['content']} "
                    f"Style: {style_desc}"
                )
                img_bytes, mime, model_used = await generate_image(prompt, style_type)
                db.set_slide_image(slide["id"], img_bytes, mime)
                logger.info(
                    "Generated slide %s image for post %s via %s",
                    slide["slide_number"], post_id, model_used,
                )

    # ── Caption generation ────────────────────────────────────────────────────
    if not post.get("caption"):
        if "linkedin_post" in channels:
            caption = await generate_linkedin_caption(post)
        else:
            slides = db.get_slides_for_post(post_id) if post["format"] == "carousel" else None
            caption = await generate_caption(post, slides)
        db.set_post_caption(post_id, caption)
        logger.info("Generated caption for post %s (%s)", post_id, post["date"])

    # ── LinkedIn article ──────────────────────────────────────────────────────
    if "linkedin_article" in channels and not post.get("linkedin_title"):
        article = await generate_linkedin_article(post)
        db.set_post_linkedin_article(post_id, article["title"], article["body"])
        logger.info("Generated LinkedIn article for post %s (%s)", post_id, post["date"])
