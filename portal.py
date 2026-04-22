"""Admin portal — FastAPI + Jinja2."""

import logging
import os

import db
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from services.content import generate_for_post, generate_reel_concept
from services.llm import build_brand_prompt, build_system_prompt, call_claude
from services.media import generate_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PORTAL_ADMIN_USERNAME = os.environ.get("PORTAL_ADMIN_USERNAME", "admin")
PORTAL_ADMIN_PASSWORD = os.environ.get("PORTAL_ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

if not SECRET_KEY:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)
    logging.getLogger(__name__).warning(
        "SECRET_KEY not set — using a random key (sessions will reset on restart). "
        "Set the SECRET_KEY environment variable for persistent sessions."
    )
if not PORTAL_ADMIN_PASSWORD:
    logging.getLogger(__name__).warning(
        "PORTAL_ADMIN_PASSWORD not set — portal login is disabled. "
        "Set the PORTAL_ADMIN_PASSWORD environment variable to enable it."
    )
APP_NAME = os.environ.get("APP_NAME", "Little Majlis")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")
templates.env.globals["app_name"] = APP_NAME

CLAUDE_MODELS = db.CLAUDE_MODELS
TOOL_SETS = ["default", "content_writer"]



def _ok(request: Request) -> bool:
    return request.session.get("logged_in", False)


def _guard(request: Request):
    if not _ok(request):
        return RedirectResponse("/login", status_code=302)


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _ok(request):
        return RedirectResponse("/posts", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == PORTAL_ADMIN_USERNAME and password == PORTAL_ADMIN_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/posts", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"}, status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/posts", status_code=302)


# ── Projects ─────────────────────────────────────────────────────────────────

@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("projects.html", {
        "request": request,
        "projects": db.get_all_projects(),
        "saved": request.query_params.get("saved") == "1",
    })


@app.get("/projects/new", response_class=HTMLResponse)
def project_new_page(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("project_form.html", {
        "request": request,
        "project": None,
        "statuses": db.PROJECT_STATUSES,
    })


@app.post("/projects/new")
def project_create(request: Request,
                   name: str = Form(...),
                   description: str = Form(""),
                   status: str = Form("planning"),
                   deadline: str = Form("")):
    if r := _guard(request): return r
    try:
        p = db.create_project(
            name.strip(),
            description.strip() or None,
            status,
            deadline.strip() or None,
        )
        return RedirectResponse(f"/projects/{p['id']}", status_code=302)
    except Exception as e:
        logger.warning("project_create error: %s", e)
        return RedirectResponse("/projects?error=Name+already+exists", status_code=302)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: int, request: Request):
    if r := _guard(request): return r
    project, tasks, posts = db.get_project(project_id)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    return templates.TemplateResponse("project_detail.html", {
        "request": request,
        "project": project,
        "tasks": tasks,
        "posts": posts,
        "statuses": db.PROJECT_STATUSES,
        "saved": request.query_params.get("saved") == "1",
    })


@app.post("/projects/{project_id}/edit")
def project_edit(project_id: int, request: Request,
                 name: str = Form(...),
                 description: str = Form(""),
                 status: str = Form("planning"),
                 deadline: str = Form("")):
    if r := _guard(request): return r
    db.update_project(
        project_id,
        name=name.strip(),
        description=description.strip() or None,
        status=status,
        deadline=deadline.strip() or None,
    )
    return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=302)


@app.post("/projects/{project_id}/delete")
def project_delete(project_id: int, request: Request):
    if r := _guard(request): return r
    db.delete_project(project_id)
    return RedirectResponse("/projects", status_code=302)


# ── Tasks (outreach pipeline) ─────────────────────────────────────────────────

@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    if r := _guard(request): return r
    statuses = ["not_started", "dm_sent", "replied", "meeting_scheduled", "confirmed", "declined"]
    return templates.TemplateResponse("tasks.html", {
        "request": request,
        "tasks": db.get_all_tasks(),
        "statuses": statuses,
        "contacts": db.get_all_contacts(),
    })


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: int, request: Request):
    if r := _guard(request): return r
    task = db.get_task(task_id)
    if not task:
        return HTMLResponse("Task not found", status_code=404)
    statuses = ["not_started", "dm_sent", "replied", "meeting_scheduled", "confirmed", "declined"]
    return templates.TemplateResponse("task_detail.html", {
        "request": request,
        "task": task,
        "contacts": db.get_all_contacts(),
        "projects": db.get_all_projects(),
        "gmail_connected": bool(db.get_connected_email_accounts()),
        "email_thread": db.get_unified_emails_for_task(task_id),
        "statuses": statuses,
        "saved": request.query_params.get("saved") == "1",
        "generating": request.query_params.get("generating") == "1",
    })


@app.post("/tasks/{task_id}/status")
def task_update_status(task_id: int, request: Request, status: str = Form(...)):
    if r := _guard(request): return r
    db.update_task_status(task_id, status)
    return RedirectResponse("/tasks", status_code=302)


@app.post("/tasks/{task_id}/link-contact")
def task_link_contact(task_id: int, request: Request, contact_id: str = Form(...)):
    if r := _guard(request): return r
    cid = int(contact_id) if contact_id else None
    db.update_task_fields(task_id, contact_id=cid)
    return RedirectResponse(f"/tasks/{task_id}?saved=1", status_code=302)


@app.post("/tasks/{task_id}/set-project")
def task_set_project(task_id: int, request: Request, project_id: str = Form(...)):
    if r := _guard(request): return r
    pid = int(project_id) if project_id else None
    db.update_task_fields(task_id, project_id=pid)
    return RedirectResponse(f"/tasks/{task_id}?saved=1", status_code=302)


@app.post("/tasks/{task_id}/save-draft")
def task_save_draft(task_id: int, request: Request,
                    email_subject: str = Form(""), email_body: str = Form("")):
    if r := _guard(request): return r
    task = db.get_task(task_id)
    if not task:
        return HTMLResponse("Task not found", status_code=404)
    current_status = task["email_status"] or "no_draft"
    new_status = "draft_ready" if current_status == "no_draft" else current_status
    db.update_task_fields(task_id, email_subject=email_subject, email_body=email_body, email_status=new_status)
    return RedirectResponse(f"/tasks/{task_id}?saved=1", status_code=302)


@app.post("/tasks/{task_id}/generate-draft")
def task_generate_draft(task_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_generate_email_draft, task_id)
    return RedirectResponse(f"/tasks/{task_id}?generating=1", status_code=302)


@app.post("/tasks/{task_id}/approve-email")
def task_approve_email(task_id: int, request: Request):
    if r := _guard(request): return r
    db.update_task_fields(task_id, email_status="approved")
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@app.post("/tasks/{task_id}/unapprove-email")
def task_unapprove_email(task_id: int, request: Request):
    if r := _guard(request): return r
    db.update_task_fields(task_id, email_status="draft_ready")
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@app.post("/tasks/{task_id}/send-email")
def task_send_email(task_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_do_send_email, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


# ── Contacts (address book) ───────────────────────────────────────────────────

@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("contacts.html", {
        "request": request,
        "contacts": db.get_all_contacts(),
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error"),
    })


@app.post("/contacts/new")
def contact_create(request: Request,
                   name: str = Form(...),
                   first_name: str = Form(""),
                   company: str = Form(""),
                   email: str = Form(""),
                   correspondence_language: str = Form("")):
    if r := _guard(request): return r
    try:
        db.create_contact(name.strip(), first_name.strip(), company.strip(), email.strip(),
                          correspondence_language.strip() or None)
    except Exception as e:
        logger.warning("contact_create error: %s", e)
        return RedirectResponse("/contacts?error=Email+already+exists", status_code=302)
    return RedirectResponse("/contacts?saved=1", status_code=302)


@app.get("/contacts/{contact_id}/edit", response_class=HTMLResponse)
def contact_edit_page(contact_id: int, request: Request):
    if r := _guard(request): return r
    contact = db.get_contact(contact_id)
    if not contact:
        return HTMLResponse("Contact not found", status_code=404)
    return templates.TemplateResponse("contact_edit.html", {
        "request": request,
        "contact": contact,
        "email_accounts": db.get_connected_email_accounts(),
        "email_thread": db.get_unified_emails_for_contact(contact_id),
        "saved": request.query_params.get("saved") == "1",
    })


@app.post("/contacts/{contact_id}/edit")
def contact_update(contact_id: int, request: Request,
                   name: str = Form(...),
                   first_name: str = Form(""),
                   company: str = Form(""),
                   email: str = Form(""),
                   correspondence_language: str = Form(""),
                   preferred_email_account_id: str = Form("")):
    if r := _guard(request): return r
    db.update_contact(contact_id, name.strip(), first_name.strip(), company.strip(), email.strip(),
                      correspondence_language.strip() or None)
    account_id = int(preferred_email_account_id) if preferred_email_account_id.strip().isdigit() else None
    db.update_contact_preferred_account(contact_id, account_id)
    return RedirectResponse(f"/contacts/{contact_id}/edit?saved=1", status_code=302)


@app.post("/contacts/{contact_id}/delete")
def contact_delete(contact_id: int, request: Request):
    if r := _guard(request): return r
    db.delete_contact(contact_id)
    return RedirectResponse("/contacts", status_code=302)


# ── Settings — Email accounts (multi-provider) ────────────────────────────────

# Legacy Gmail redirects
@app.get("/settings/gmail")
def settings_gmail_redirect(request: Request):
    return RedirectResponse("/settings/email", status_code=301)

@app.get("/settings/gmail/{path:path}")
def settings_gmail_path_redirect(path: str, request: Request):
    return RedirectResponse(f"/settings/email/{path}", status_code=301)


@app.get("/settings/email", response_class=HTMLResponse)
def settings_email(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("settings_email.html", {
        "request": request,
        "accounts": db.get_all_email_accounts(),
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error"),
    })


@app.post("/settings/email/new")
def settings_email_new(request: Request,
                       label: str = Form(""),
                       provider: str = Form("gmail"),
                       client_id: str = Form(...),
                       client_secret: str = Form(...),
                       callback_url: str = Form(""),
                       tenant_id: str = Form("")):
    if r := _guard(request): return r
    account_id = db.create_email_account(
        label.strip(), client_id.strip(), client_secret.strip(),
        provider=provider, callback_url=callback_url.strip() or None,
        tenant_id=tenant_id.strip() or None,
    )
    return RedirectResponse(f"/settings/email/{account_id}?saved=1", status_code=302)


# NOTE: callback routes must be defined BEFORE /settings/email/{account_id}
@app.get("/settings/email/callback/gmail")
def settings_email_callback_gmail(request: Request, code: str = "", error: str = "",
                                   state: str = ""):
    if r := _guard(request): return r
    if error or not code:
        return RedirectResponse(f"/settings/email?error={error or 'Auth+cancelled'}", status_code=302)
    try:
        account_id = int(state)
    except (ValueError, TypeError):
        return RedirectResponse("/settings/email?error=Invalid+state", status_code=302)
    from services.gmail import exchange_code
    ok = exchange_code(code, account_id)
    if not ok:
        return RedirectResponse(f"/settings/email/{account_id}?error=Token+exchange+failed", status_code=302)
    return RedirectResponse(f"/settings/email/{account_id}?saved=1", status_code=302)


@app.get("/settings/email/callback/outlook")
def settings_email_callback_outlook(request: Request, code: str = "", error: str = "",
                                     state: str = ""):
    if r := _guard(request): return r
    if error or not code:
        return RedirectResponse(f"/settings/email?error={error or 'Auth+cancelled'}", status_code=302)
    try:
        account_id = int(state)
    except (ValueError, TypeError):
        return RedirectResponse("/settings/email?error=Invalid+state", status_code=302)
    from services.outlook import exchange_code
    ok = exchange_code(code, account_id)
    if not ok:
        return RedirectResponse(f"/settings/email/{account_id}?error=Token+exchange+failed", status_code=302)
    return RedirectResponse(f"/settings/email/{account_id}?saved=1", status_code=302)


@app.get("/settings/email/{account_id}", response_class=HTMLResponse)
def settings_email_account(account_id: int, request: Request):
    if r := _guard(request): return r
    account = db.get_email_account(account_id)
    if not account:
        return HTMLResponse("Account not found", status_code=404)
    from services.gmail import FALLBACK_REDIRECT_URI as GMAIL_FALLBACK
    from services.outlook import FALLBACK_REDIRECT_URI as OUTLOOK_FALLBACK
    return templates.TemplateResponse("settings_email_account.html", {
        "request": request,
        "account": account,
        "gmail_fallback_uri": GMAIL_FALLBACK,
        "outlook_fallback_uri": OUTLOOK_FALLBACK,
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error"),
    })


@app.post("/settings/email/{account_id}/config")
def settings_email_save_config(account_id: int, request: Request,
                                label: str = Form(""),
                                client_id: str = Form(...),
                                client_secret: str = Form(...),
                                callback_url: str = Form(""),
                                tenant_id: str = Form("")):
    if r := _guard(request): return r
    db.save_email_config(account_id, label.strip(), client_id.strip(),
                         client_secret.strip(), callback_url.strip() or None,
                         tenant_id.strip() or None)
    return RedirectResponse(f"/settings/email/{account_id}?saved=1", status_code=302)


@app.get("/settings/email/{account_id}/connect")
def settings_email_connect(account_id: int, request: Request):
    if r := _guard(request): return r
    account = db.get_email_account(account_id)
    if not account:
        return RedirectResponse("/settings/email?error=Account+not+found", status_code=302)
    provider = account.get("provider", "gmail")
    if provider == "gmail":
        from services.gmail import get_auth_url
    elif provider == "outlook":
        from services.outlook import get_auth_url
    else:
        return RedirectResponse(f"/settings/email/{account_id}?error=Unknown+provider", status_code=302)
    url = get_auth_url(account_id)
    if not url:
        return RedirectResponse(
            f"/settings/email/{account_id}?error=Save+client+credentials+first",
            status_code=302,
        )
    return RedirectResponse(url, status_code=302)


@app.post("/settings/email/{account_id}/disconnect")
def settings_email_disconnect(account_id: int, request: Request):
    if r := _guard(request): return r
    db.clear_email_tokens(account_id)
    return RedirectResponse(f"/settings/email/{account_id}", status_code=302)


@app.post("/settings/email/{account_id}/set-default")
def settings_email_set_default(account_id: int, request: Request):
    if r := _guard(request): return r
    db.set_email_account_default(account_id)
    return RedirectResponse(f"/settings/email/{account_id}?saved=1", status_code=302)


@app.post("/settings/email/{account_id}/delete")
def settings_email_delete(account_id: int, request: Request):
    if r := _guard(request): return r
    db.delete_email_account(account_id)
    return RedirectResponse("/settings/email", status_code=302)


# ── Posts ─────────────────────────────────────────────────────────────────────

@app.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request, generating: str = "", channel: str = ""):
    if r := _guard(request): return r
    all_posts = db.get_all_posts()
    if channel:
        all_posts = [p for p in all_posts if channel in (p.get("channels") or "instagram").split(",")]
    return templates.TemplateResponse("posts.html", {
        "request": request,
        "posts": all_posts,
        "generating": generating == "1",
    })


@app.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(post_id: int, request: Request):
    if r := _guard(request): return r
    post = db.get_post(post_id)
    if not post:
        return HTMLResponse("Post not found", status_code=404)
    slides = db.get_slides_for_post(post_id) if post["format"] == "carousel" else []
    channels = [c.strip() for c in (post.get("channels") or "instagram").split(",")]
    linkedin_connected = bool(db.get_connected_linkedin_accounts())
    return templates.TemplateResponse("post_detail.html", {
        "request": request,
        "post": post,
        "slides": slides,
        "channels": channels,
        "linkedin_connected": linkedin_connected,
        "projects": db.get_all_projects(),
        "saved": request.query_params.get("saved") == "1",
    })


@app.post("/posts/{post_id}/set-project")
def post_set_project(post_id: int, request: Request, project_id: str = Form(...)):
    if r := _guard(request): return r
    pid = int(project_id) if project_id else None
    db.set_post_project(post_id, pid)
    return RedirectResponse(f"/posts/{post_id}?saved=1", status_code=302)


@app.post("/posts/{post_id}/generate-linkedin")
def generate_linkedin(post_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_generate_linkedin_for_post, post_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


@app.post("/posts/{post_id}/post-linkedin")
def post_to_linkedin(post_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_do_post_linkedin, post_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


@app.post("/posts/{post_id}/reel-concept")
def reel_concept(post_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_generate_reel_concept_for_post, post_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


@app.post("/posts/{post_id}/generate-video")
def generate_video_route(post_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_generate_video_for_post, post_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


@app.post("/posts/{post_id}/approve")
def approve_post(post_id: int, request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    db.set_post_approved(post_id, True)
    post = db.get_post(post_id)
    if post and (not post["image_bytes"] or not post["caption"]):
        background_tasks.add_task(_generate_for_post_id, post_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


@app.post("/posts/{post_id}/unapprove")
def unapprove_post(post_id: int, request: Request):
    if r := _guard(request): return r
    db.set_post_approved(post_id, False)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


@app.post("/posts/{post_id}/style")
def update_style(post_id: int, request: Request, background_tasks: BackgroundTasks,
                 image_style_type: str = Form(...)):
    if r := _guard(request): return r
    post = db.get_post(post_id)
    if post and post["image_style_type"] != image_style_type:
        db.update_post_fields(str(post["date"]), image_style_type=image_style_type)
        db.clear_post_images(post_id)
        background_tasks.add_task(_generate_for_post_id, post_id)
    return RedirectResponse(f"/posts/{post_id}", status_code=302)


# ── Image serving ─────────────────────────────────────────────────────────────

@app.get("/posts/{post_id}/image")
def post_image(post_id: int, request: Request):
    if not _ok(request): return Response(status_code=401)
    post = db.get_post(post_id)
    if not post or not post["image_bytes"]:
        return Response(status_code=404)
    return Response(content=bytes(post["image_bytes"]), media_type=post["image_mime_type"] or "image/jpeg")


@app.get("/slides/{slide_id}/image")
def slide_image(slide_id: int, request: Request):
    if not _ok(request): return Response(status_code=401)
    slide = db.get_slide(slide_id)
    if not slide or not slide["image_bytes"]:
        return Response(status_code=404)
    return Response(content=bytes(slide["image_bytes"]), media_type=slide["image_mime_type"] or "image/jpeg")


@app.get("/posts/{post_id}/video")
def post_video(post_id: int, request: Request):
    if not _ok(request): return Response(status_code=401)
    post = db.get_post(post_id)
    if not post or not post["video_bytes"]:
        return Response(status_code=404)
    return Response(
        content=bytes(post["video_bytes"]),
        media_type=post["video_mime_type"] or "video/mp4",
        headers={"Content-Disposition": f"inline; filename=reel_{post_id}.mp4"},
    )


# ── Generation ────────────────────────────────────────────────────────────────

_generating = False


@app.post("/generate-pending")
def generate_pending(request: Request, background_tasks: BackgroundTasks):
    if r := _guard(request): return r
    background_tasks.add_task(_generate_all_pending)
    return RedirectResponse("/posts?generating=1", status_code=302)


async def _generate_for_post_id(post_id: int):
    post = db.get_post(post_id)
    if post:
        await generate_for_post(dict(post))


async def _generate_all_pending():
    global _generating
    if _generating:
        return
    _generating = True
    try:
        for post in db.get_posts_pending_generation():
            try:
                await generate_for_post(dict(post))
            except Exception as e:
                logger.error("Generation failed for post %s (%s): %s", post["id"], post["date"], e)
    finally:
        _generating = False


async def _generate_reel_concept_for_post(post_id: int):
    post = db.get_post(post_id)
    if not post or post["format"] != "reel":
        return
    try:
        video_prompt = await generate_reel_concept(dict(post))
        db.set_post_video_prompt(post_id, video_prompt)
        logger.info("Generated reel concept for post %s (%s)", post_id, post["date"])
    except Exception as e:
        logger.error("Reel concept generation failed for post %s: %s", post_id, e)


async def _generate_video_for_post(post_id: int):
    post = db.get_post(post_id)
    if not post or post["format"] != "reel" or not post.get("video_prompt"):
        return
    try:
        video_bytes, mime = await generate_video(post["video_prompt"])
        db.set_post_video(post_id, video_bytes, mime)
        logger.info("Generated video for post %s (%s)", post_id, post["date"])
    except Exception as e:
        logger.error("Video generation failed for post %s: %s", post_id, e)


async def _generate_linkedin_for_post(post_id: int):
    from services.content import generate_linkedin_caption, generate_linkedin_article
    post = db.get_post(post_id)
    if not post:
        return
    channels = [c.strip() for c in (post.get("channels") or "instagram").split(",")]
    try:
        if "linkedin_post" in channels:
            caption = await generate_linkedin_caption(dict(post))
            db.set_post_linkedin_caption(post_id, caption)
            logger.info("Generated LinkedIn caption for post %d", post_id)
        if "linkedin_article" in channels:
            article = await generate_linkedin_article(dict(post))
            db.set_post_linkedin_article(post_id, article["title"], article["body"])
            logger.info("Generated LinkedIn article for post %d", post_id)
    except Exception as e:
        logger.error("LinkedIn generation failed for post %d: %s", post_id, e)


async def _do_post_linkedin(post_id: int):
    from services.linkedin import create_post, _first_connected_account_id
    post = db.get_post(post_id)
    if not post:
        return
    account_id = _first_connected_account_id()
    if not account_id:
        logger.error("post_linkedin: no connected LinkedIn account")
        return
    channels = [c.strip() for c in (post.get("channels") or "instagram").split(",")]
    image_bytes = bytes(post["image_bytes"]) if post.get("image_bytes") else None
    mime_type = post.get("image_mime_type") or "image/jpeg"
    try:
        if "linkedin_post" in channels and post.get("linkedin_caption"):
            result = create_post(account_id, post["linkedin_caption"], image_bytes, mime_type)
            if result:
                db.mark_post_linkedin_sent(post_id)
                logger.info("Posted to LinkedIn for post %d: %s", post_id, result)
    except Exception as e:
        logger.error("LinkedIn posting failed for post %d: %s", post_id, e)


async def _generate_email_draft(task_id: int):
    task = db.get_task(task_id)
    if not task:
        return
    try:
        contact_info = ""
        if task.get("contact_name"):
            parts = []
            if task.get("contact_first_name"):
                parts.append(f"First name: {task['contact_first_name']}")
            if task.get("contact_company"):
                parts.append(f"Company: {task['contact_company']}")
            if task.get("contact_email"):
                parts.append(f"Email: {task['contact_email']}")
            contact_info = f"\nContact: {task['contact_name']}\n" + "\n".join(parts)

        system = build_brand_prompt()
        prompt = (
            f"Write a professional outreach email for the following task:\n\n"
            f"Task: {task['name']}\n"
            f"Priority: {task.get('priority') or 'MEDIUM'}\n"
            f"Notes: {task.get('notes') or 'None'}\n"
            f"{contact_info}\n\n"
            "Return ONLY the email body (no subject line, no 'Subject:' prefix). "
            "The tone should match the brand voice: warm, personal, never pushy. "
            "Write in the language most appropriate for the recipient. "
            "Keep it under 200 words."
        )
        response = await call_claude(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        body = response.content[0].text.strip()
        subject = task.get("email_subject") or task["name"]
        db.update_task_fields(task_id, email_body=body, email_subject=subject, email_status="draft_ready")
        logger.info("Generated email draft for task %d", task_id)
    except Exception as e:
        logger.error("Email draft generation failed for task %d: %s", task_id, e)


async def _do_send_email(task_id: int):
    from services.gmail import send_email  # Portal approval flow uses Gmail's task-based sender
    ok = send_email(task_id)
    if ok:
        logger.info("Email sent for task %d", task_id)
    else:
        logger.error("Email send failed for task %d", task_id)


# ── Team (agents) ─────────────────────────────────────────────────────────────

@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("team.html", {
        "request": request, "agents": db.get_all_agents(),
    })


@app.get("/team/new", response_class=HTMLResponse)
def agent_new(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("agent_edit.html", {
        "request": request, "agent": None, "models": CLAUDE_MODELS,
        "tool_sets": TOOL_SETS, "error": None,
    })


@app.post("/team/new")
def agent_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(...),
    model: str = Form("claude-sonnet-4-6"),
    tool_set: str = Form("default"),
):
    if r := _guard(request): return r
    if model not in CLAUDE_MODELS:
        return templates.TemplateResponse("agent_edit.html", {
            "request": request, "agent": None, "models": CLAUDE_MODELS,
            "tool_sets": TOOL_SETS, "error": "Invalid model selected.",
        })
    if tool_set not in TOOL_SETS:
        tool_set = "default"
    db.upsert_agent(name.strip().lower(), description, system_prompt, model, is_router=False, tool_set=tool_set)
    return RedirectResponse("/team", status_code=302)


@app.get("/team/{agent_id}/edit", response_class=HTMLResponse)
def agent_edit(agent_id: int, request: Request):
    if r := _guard(request): return r
    agent = db.get_agent(agent_id)
    if not agent:
        return HTMLResponse("Agent not found", status_code=404)
    return templates.TemplateResponse("agent_edit.html", {
        "request": request, "agent": agent, "models": CLAUDE_MODELS,
        "tool_sets": TOOL_SETS, "error": None,
    })


@app.post("/team/{agent_id}/edit")
def agent_update(
    agent_id: int,
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    system_prompt: str = Form(...),
    model: str = Form("claude-sonnet-4-6"),
    tool_set: str = Form("default"),
):
    if r := _guard(request): return r
    if model not in CLAUDE_MODELS:
        agent = db.get_agent(agent_id)
        return templates.TemplateResponse("agent_edit.html", {
            "request": request, "agent": agent, "models": CLAUDE_MODELS,
            "tool_sets": TOOL_SETS, "error": "Invalid model selected.",
        })
    if tool_set not in TOOL_SETS:
        tool_set = "default"
    db.update_agent(agent_id, description, system_prompt, model, name=name or None, tool_set=tool_set)
    return RedirectResponse("/team", status_code=302)


@app.post("/team/{agent_id}/delete")
def agent_delete(agent_id: int, request: Request):
    if r := _guard(request): return r
    db.delete_agent(agent_id)
    return RedirectResponse("/team", status_code=302)


# ── Brand guidelines ──────────────────────────────────────────────────────────

@app.get("/brand", response_class=HTMLResponse)
def brand_page(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("brand.html", {
        "request": request, "content": db.get_brand_guidelines(),
    })


@app.post("/brand")
def brand_update(request: Request, content: str = Form(...)):
    if r := _guard(request): return r
    db.update_brand_guidelines(content)
    return RedirectResponse("/brand?saved=1", status_code=302)


# ── Email log ─────────────────────────────────────────────────────────────────

@app.get("/emails", response_class=HTMLResponse)
def emails_page(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("emails.html", {
        "request": request,
        "emails": db.get_unified_email_log(),
    })


# ── Settings (agent preferences) ─────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if r := _guard(request): return r
    enabled_raw = db.get_setting("enabled_channels", "instagram")
    enabled = [c.strip() for c in enabled_raw.split(",") if c.strip()]
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "owner_name": db.get_setting("owner_name", ""),
        "agent_language": db.get_setting("agent_language", "en"),
        "enabled_channels": enabled,
        "allowed_user_ids": db.get_setting("allowed_user_ids", ""),
        "google_ai_studio_key": db.get_setting("google_ai_studio_key", ""),
        "gemini_image_model": db.get_setting(
            "gemini_image_model",
            "gemini-3.1-flash-image-preview,gemini-2.5-flash-image",
        ),
        "saved": request.query_params.get("saved") == "1",
    })


@app.post("/settings")
async def settings_update(request: Request):
    if r := _guard(request): return r
    form = await request.form()
    agent_language = (form.get("agent_language") or "en").strip().lower()[:10]
    db.set_setting("agent_language", agent_language)
    channels = form.getlist("channels")
    if not channels:
        channels = ["instagram"]
    db.set_setting("enabled_channels", ",".join(channels))
    return RedirectResponse("/settings?saved=1", status_code=302)


@app.post("/settings/allowed-users")
def settings_save_allowed_users(request: Request, allowed_user_ids: str = Form("")):
    if r := _guard(request): return r
    db.set_setting("allowed_user_ids", allowed_user_ids.strip())
    return RedirectResponse("/settings?saved=1", status_code=302)


@app.post("/settings/google-ai-key")
def settings_save_google_ai_key(request: Request, google_ai_studio_key: str = Form("")):
    if r := _guard(request): return r
    db.set_setting("google_ai_studio_key", google_ai_studio_key.strip())
    return RedirectResponse("/settings?saved=1", status_code=302)


@app.post("/settings/owner-name")
def settings_save_owner_name(request: Request, owner_name: str = Form("")):
    if r := _guard(request): return r
    db.set_setting("owner_name", owner_name.strip())
    return RedirectResponse("/settings?saved=1", status_code=302)


@app.post("/settings/gemini-models")
def settings_save_gemini_models(request: Request, gemini_image_model: str = Form("")):
    if r := _guard(request): return r
    db.set_setting("gemini_image_model", gemini_image_model.strip())
    return RedirectResponse("/settings?saved=1", status_code=302)


# ── Settings — LinkedIn ───────────────────────────────────────────────────────

@app.get("/settings/linkedin", response_class=HTMLResponse)
def settings_linkedin(request: Request):
    if r := _guard(request): return r
    return templates.TemplateResponse("settings_linkedin.html", {
        "request": request,
        "accounts": db.get_all_linkedin_accounts(),
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error"),
    })


@app.post("/settings/linkedin/new")
def settings_linkedin_new(request: Request,
                           label: str = Form(""),
                           client_id: str = Form(...),
                           client_secret: str = Form(...),
                           callback_url: str = Form("")):
    if r := _guard(request): return r
    account_id = db.create_linkedin_account(
        label.strip(), client_id.strip(), client_secret.strip(), callback_url.strip() or None
    )
    return RedirectResponse(f"/settings/linkedin/{account_id}?saved=1", status_code=302)


# NOTE: /settings/linkedin/callback must be defined BEFORE /settings/linkedin/{account_id}
@app.get("/settings/linkedin/callback")
def settings_linkedin_callback(request: Request, code: str = "", error: str = "",
                                state: str = ""):
    if r := _guard(request): return r
    if error or not code:
        return RedirectResponse(f"/settings/linkedin?error={error or 'Auth+cancelled'}", status_code=302)
    try:
        account_id = int(state)
    except (ValueError, TypeError):
        return RedirectResponse("/settings/linkedin?error=Invalid+state", status_code=302)
    from services.linkedin import exchange_code
    ok = exchange_code(code, account_id)
    if not ok:
        return RedirectResponse(f"/settings/linkedin/{account_id}?error=Token+exchange+failed", status_code=302)
    return RedirectResponse(f"/settings/linkedin/{account_id}?saved=1", status_code=302)


@app.get("/settings/linkedin/{account_id}", response_class=HTMLResponse)
def settings_linkedin_account(account_id: int, request: Request):
    if r := _guard(request): return r
    from services.linkedin import FALLBACK_REDIRECT_URI
    account = db.get_linkedin_account(account_id)
    if not account:
        return HTMLResponse("Account not found", status_code=404)
    return templates.TemplateResponse("settings_linkedin_account.html", {
        "request": request,
        "account": account,
        "fallback_redirect_uri": FALLBACK_REDIRECT_URI,
        "saved": request.query_params.get("saved") == "1",
        "error": request.query_params.get("error"),
    })


@app.post("/settings/linkedin/{account_id}/config")
def settings_linkedin_save_config(account_id: int, request: Request,
                                   label: str = Form(""),
                                   client_id: str = Form(...),
                                   client_secret: str = Form(...),
                                   callback_url: str = Form("")):
    if r := _guard(request): return r
    db.save_linkedin_config(account_id, label.strip(), client_id.strip(),
                            client_secret.strip(), callback_url.strip() or None)
    return RedirectResponse(f"/settings/linkedin/{account_id}?saved=1", status_code=302)


@app.get("/settings/linkedin/{account_id}/connect")
def settings_linkedin_connect(account_id: int, request: Request):
    if r := _guard(request): return r
    from services.linkedin import get_auth_url
    url = get_auth_url(account_id)
    if not url:
        return RedirectResponse(
            f"/settings/linkedin/{account_id}?error=Save+client+credentials+first",
            status_code=302,
        )
    return RedirectResponse(url, status_code=302)


@app.post("/settings/linkedin/{account_id}/disconnect")
def settings_linkedin_disconnect(account_id: int, request: Request):
    if r := _guard(request): return r
    db.clear_linkedin_tokens(account_id)
    return RedirectResponse(f"/settings/linkedin/{account_id}", status_code=302)


@app.post("/settings/linkedin/{account_id}/delete")
def settings_linkedin_delete(account_id: int, request: Request):
    if r := _guard(request): return r
    db.delete_linkedin_account(account_id)
    return RedirectResponse("/settings/linkedin", status_code=302)
