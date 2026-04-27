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
    if post["format"] == "static" and not post.get("image_locked"):
        if not post.get("has_image") and not post.get("image_bytes") and post.get("image_prompt"):
            img_bytes, mime, model_used = await generate_image(post["image_prompt"], style_type)
            db.set_post_image(post_id, img_bytes, mime, model_used, post["image_prompt"])
            logger.info("Generated image for post %s (%s) via %s", post_id, post["date"], model_used)

    elif post["format"] == "carousel" and not post.get("image_locked"):
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
    if not post.get("caption") and not post.get("caption_locked"):
        if post["format"] == "carousel":
            full_slides = db.get_slides_for_post(post_id)
            caption = await generate_caption(post, full_slides)
        else:
            caption = await generate_caption(post)
        db.set_post_caption(post_id, caption)
        logger.info("Generated caption for post %s (%s)", post_id, post["date"])

    if post["format"] == "reel":
        reel_generator = post.get("reel_generator") or "veo"

        if reel_generator == "canva" and not post.get("video_bytes"):
            await _generate_canva_reel(post)
        elif reel_generator == "veo" and post.get("video_prompt") and not post.get("video_bytes"):
            from services.media import generate_video
            video_bytes, mime = await generate_video(post["video_prompt"])
            db.set_post_video(post_id, video_bytes, mime)
            logger.info("Generated Veo video for post %s (%s)", post_id, post["date"])

    # ── LinkedIn content ──────────────────────────────────────────────────────

    if "linkedin_post" in channels and not post.get("linkedin_caption"):
        li_caption = await generate_linkedin_caption(post)
        db.set_post_linkedin_caption(post_id, li_caption)
        logger.info("Generated LinkedIn caption for post %s (%s)", post_id, post["date"])

    # ── LinkedIn article ──────────────────────────────────────────────────────
    if "linkedin_article" in channels and not post.get("linkedin_title"):
        article = await generate_linkedin_article(post)
        db.set_post_linkedin_article(post_id, article["title"], article["body"])
        logger.info("Generated LinkedIn article for post %s (%s)", post_id, post["date"])


async def _generate_canva_reel(post: dict) -> None:
    """Generate a Canva reel: Imagen background → Canva asset → autofill → export → store."""
    import asyncio
    from services.media import generate_image
    from services import canva

    post_id = post["id"]
    template_id = post.get("canva_template_id")
    if not template_id:
        logger.warning("Canva reel requested for post %s but no canva_template_id set", post_id)
        return

    mappings = db.get_canva_field_mappings(template_id)
    if not mappings:
        logger.warning("No field mappings configured for Canva template %s", template_id)
        return

    # Generate background image via Imagen
    image_prompt = post.get("image_prompt") or post.get("theme", "")
    img_bytes, img_mime, model_used = await generate_image(image_prompt, "realistic")
    logger.info("Generated background image for Canva reel post %s via %s", post_id, model_used)

    # Upload image to Canva as an asset
    asset_id = await asyncio.to_thread(canva.upload_asset, img_bytes, img_mime, "background")
    if not asset_id:
        logger.error("Canva asset upload failed for post %s", post_id)
        return

    # Build autofill payload from field mappings
    fields = {}
    for m in mappings:
        field_name = m["canva_field_name"]
        mapped_to = m["mapped_to"]
        if mapped_to == "image":
            fields[field_name] = asset_id
        elif mapped_to == "theme":
            fields[field_name] = post.get("theme", "")

    # Autofill template → new design ID
    design_id = await asyncio.to_thread(canva.autofill, template_id, fields)
    if not design_id:
        logger.error("Canva autofill failed for post %s", post_id)
        return
    logger.info("Canva autofill complete for post %s, design_id=%s", post_id, design_id)

    # Export as MP4
    video_bytes = await asyncio.to_thread(canva.export_video, design_id)
    if not video_bytes:
        logger.error("Canva export failed for post %s", post_id)
        return

    db.set_post_video(post_id, video_bytes, "video/mp4")
    logger.info("Stored Canva MP4 for post %s (%d bytes)", post_id, len(video_bytes))
