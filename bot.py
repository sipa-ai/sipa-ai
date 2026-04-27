"""Multi-agent assistant bot — all messages route through the router agent."""

import asyncio
import json
import logging
import os
import re as _re
from datetime import date
from io import BytesIO

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
from services.contacts import tasks_to_json, posts_to_json
from services.llm import build_system_prompt, call_claude
from services.media import send_post_media

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


def _allowed_user_ids() -> list[int]:
    raw = db.get_setting("allowed_user_ids", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]

# ── Conversation history (router only — it is the single entry point) ─────────

conversation_history: dict[int, list[dict]] = {}
MAX_HISTORY = 20


def _get_history(chat_id: int) -> list[dict]:
    return conversation_history.get(chat_id, [])


def _add_to_history(chat_id: int, role: str, content: str | list) -> None:
    h = conversation_history.setdefault(chat_id, [])
    h.append({"role": role, "content": content})
    # Trim: keep last MAX_HISTORY messages, ensure starts with user
    if len(h) > MAX_HISTORY:
        h[:] = h[-MAX_HISTORY:]
        while h and h[0]["role"] != "user":
            h.pop(0)


# ── Tool definitions ──────────────────────────────────────────────────────────

def _build_router_tools(owner_name: str) -> list:
    return [
    {
        "name": "delegate_to_agent",
        "description": (
            "Delegate a task to a specialist agent listed in AVAILABLE AGENTS. "
            "Include the full conversation context in the message so the specialist has everything they need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "The specialist agent's name/slug"},
                "message": {"type": "string", "description": "Full context + task for the specialist"},
            },
            "required": ["agent_name", "message"],
        },
    },
    {
        "name": "get_brand",
        "description": "Read the current brand guidelines.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_brand",
        "description": f"Replace the brand guidelines with new content. Only call when {owner_name} explicitly asks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The full new brand guidelines text"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_all_tasks",
        "description": "Read all tasks with their current status, deadlines, and project assignment.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_task_status",
        "description": "Update the status of a task by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["not_started", "dm_sent", "replied", "meeting_scheduled", "confirmed", "declined"],
                },
            },
            "required": ["name", "status"],
        },
    },
    {
        "name": "get_all_posts",
        "description": "Read all planned posts (summary, no image data).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_post_by_date",
        "description": "Read a specific post by date.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "ISO date YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "send_post_image",
        "description": "Send the generated image(s) of a post to the user in Telegram.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "ISO date YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "get_contact_by_name",
        "description": (
            "Look up a contact in the address book by name, first name, or company. "
            "Returns their email address and correspondence_language so you can draft correctly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name, first name, or company to search for"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_email_history",
        "description": (
            "Get the full email history (sent + received) for a contact. "
            "Use this to find the email to reply to — the response includes inbox_id for received emails."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "Contact ID from get_contact_by_name"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "send_approved_email",
        "description": (
            f"Send an email AFTER {owner_name} has explicitly approved it. "
            "NEVER call this without explicit user confirmation ('send', 'yes', 'go ahead'). "
            "Include a one-sentence summary in agent_language for the history log. "
            "To send a threaded reply, set reply_to_inbox_id to the inbox_id of the received email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to_email": {"type": "string", "description": "Recipient email address"},
                "to_name": {"type": "string", "description": "Recipient display name"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Full email body in recipient's correspondence_language"},
                "summary": {"type": "string", "description": "One-sentence summary of this email in agent_language"},
                "contact_id": {"type": "integer", "description": "Contact ID if known (optional)"},
                "task_id": {"type": "integer", "description": "Task ID to update email_status to sent (optional)"},
                "reply_to_inbox_id": {"type": "integer", "description": "inbox_id of the received email to reply to (optional, for threaded replies)"},
            },
            "required": ["to_email", "to_name", "subject", "body", "summary"],
        },
    },
    {
        "name": "create_agent",
        "description": (
            "Create a new specialist agent. "
            "Generate a complete system_prompt from the user's description, informed by the brand context. "
            "If name or model is not provided, ask the user first. Suggest claude-sonnet-4-6 as default model. "
            "Show the draft system_prompt for confirmation before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent slug — lowercase, no spaces (e.g. 'omar')"},
                "description": {"type": "string", "description": "One-line description shown in routing context"},
                "system_prompt": {"type": "string", "description": "Complete system prompt generated from the user's description"},
                "model": {"type": "string", "description": "Model ID — default: claude-sonnet-4-6"},
                "tool_set": {
                    "type": "string",
                    "enum": ["default", "content_writer", "website"],
                    "description": "Tool set for the agent. 'default' = read-only (tasks, posts). 'content_writer' = can also create and update posts. 'website' = can clone, edit, and push to the configured GitHub website repo.",
                },
            },
            "required": ["name", "description", "system_prompt", "model"],
        },
    },
    {
        "name": "update_agent",
        "description": (
            "Update a specialist agent's description, system_prompt, or model. "
            "Show the proposed change to the user before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name/slug to update"},
                "field": {
                    "type": "string",
                    "enum": ["description", "system_prompt", "model"],
                    "description": "Which field to update",
                },
                "new_value": {"type": "string", "description": "New value for the field"},
            },
            "required": ["name", "field", "new_value"],
        },
    },
    {
        "name": "delete_agent",
        "description": (
            "Delete a specialist agent. "
            "Always ask for explicit confirmation before calling this tool. "
            "Never delete the router/assistant agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name/slug to delete"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_project",
        "description": "Create a new project. Show the details to the user for confirmation before calling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name (unique)"},
                "description": {"type": "string", "description": "Short description (optional)"},
                "status": {
                    "type": "string",
                    "enum": ["planning", "active", "completed", "archived"],
                    "description": "Project status (default: planning)",
                },
                "deadline": {"type": "string", "description": "ISO date YYYY-MM-DD (optional)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "rename_project",
        "description": "Rename an existing project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current project name"},
                "new_name": {"type": "string", "description": "New project name"},
            },
            "required": ["name", "new_name"],
        },
    },
    {
        "name": "delete_project",
        "description": (
            "Delete a project. Tasks assigned to it will become unassigned. "
            "Always ask for explicit confirmation before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name to delete"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "assign_task_to_project",
        "description": "Assign a task to a project by name. Pass project_name as empty string to unassign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_name": {"type": "string", "description": "Task name"},
                "project_name": {"type": "string", "description": "Project name (or empty to unassign)"},
            },
            "required": ["task_name", "project_name"],
        },
    },
    {
        "name": "assign_post_to_project",
        "description": "Assign a post to a project by date. Pass project_name as empty string to unassign.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Post date ISO YYYY-MM-DD"},
                "project_name": {"type": "string", "description": "Project name (or empty to unassign)"},
            },
            "required": ["date", "project_name"],
        },
    },
    ]


# Tools for content-creation specialists (create/update posts).
# Map agent names to this list in _AGENT_TOOLS to grant access.
_CONTENT_TOOLS = [
    {
        "name": "create_post",
        "description": (
            "Save a new post to the database as a draft. "
            "IMPORTANT: Always show the full proposal to the user as text first and wait for "
            "explicit confirmation (e.g. 'yes', 'save it', 'looks good') before calling this tool. "
            "Never call this automatically when brainstorming or proposing ideas. "
            "Also call get_post_by_date first — if a post already exists for that date, "
            "tell the user and do NOT overwrite it without their explicit instruction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "format": {"type": "string", "enum": ["static", "carousel", "reel"]},
                "pillar": {"type": "string"},
                "theme": {"type": "string"},
                "image_prompt": {"type": "string", "description": "Detailed image generation prompt (static and carousel only)"},
                "image_style": {"type": "string", "description": "Visual style for carousel slides"},
                "image_style_type": {
                    "type": "string",
                    "enum": ["realistic", "artistic"],
                    "description": "realistic=photographic, artistic=illustrated/creative (static and carousel only)",
                },
                "channels": {
                    "type": "string",
                    "description": "Comma-separated publish destinations. Valid values: instagram, linkedin_post, linkedin_article. Default: instagram. Examples: 'instagram', 'linkedin_post', 'instagram,linkedin_post'",
                },
                "n_slides": {"type": "integer"},
                "video_prompt": {"type": "string", "description": "Video generation prompt (reel only, Veo). Cinematic, 9:16, 8s."},
                "reel_generator": {"type": "string", "enum": ["veo", "canva"], "description": "Which generator to use for reels. Default: veo."},
                "canva_template_id": {"type": "string", "description": "Canva design ID to use when reel_generator=canva."},
                "slides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "number": {"type": "integer"},
                            "title_en": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["number", "title_en", "content"],
                    },
                },
            },
            "required": ["date", "format", "pillar", "theme"],
        },
    },
    {
        "name": "get_post_by_date",
        "description": "Read a post by date.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
        },
    },
    {
        "name": "update_post",
        "description": "Update one or more fields of an existing post by id. Use get_all_posts to find the id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "Post id (from get_all_posts)"},
                "date": {"type": "string", "description": "New date ISO YYYY-MM-DD (to move the post)"},
                "format": {"type": "string", "enum": ["static", "carousel", "reel"]},
                "caption": {"type": "string"},
                "theme": {"type": "string"},
                "image_prompt": {"type": "string"},
                "image_style_type": {"type": "string", "enum": ["realistic", "artistic"]},
                "pillar": {"type": "string"},
                "channels": {"type": "string", "description": "Comma-separated: instagram, linkedin_post, linkedin_article"},
                "video_prompt": {"type": "string", "description": "Video generation prompt (reel only, Veo)"},
                "reel_generator": {"type": "string", "enum": ["veo", "canva"], "description": "Which generator to use for reels."},
                "canva_template_id": {"type": "string", "description": "Canva design ID (when reel_generator=canva)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_all_tasks",
        "description": "Read all tasks.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_all_posts",
        "description": "Read all planned posts.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

_DEFAULT_AGENT_TOOLS = [
    {
        "name": "get_all_tasks",
        "description": "Read all tasks with their current status, deadlines, and project assignment.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_all_posts",
        "description": "Read all planned posts (summary, no image data).",
        "input_schema": {"type": "object", "properties": {}},
    },
]

_WEBSITE_TOOLS = [
    {
        "name": "website_list_files",
        "description": (
            "Clone the configured website repo and return the file tree. "
            "Call this first to start a website editing session. "
            "Returns a session_id to pass to all subsequent website tools."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "website_read_file",
        "description": "Read a file from the website repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID from website_list_files"},
                "path": {"type": "string", "description": "File path relative to repo root"},
            },
            "required": ["session_id", "path"],
        },
    },
    {
        "name": "website_edit_file",
        "description": (
            "Write new content to a file in the website repo. "
            "Returns a text diff (HTML stripped) showing what changed. "
            "Show this diff to the user, then call website_commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Full new file content"},
            },
            "required": ["session_id", "path", "content"],
        },
    },
    {
        "name": "website_commit",
        "description": "Commit and push all edited files to GitHub, then clean up the session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string", "description": "Commit message describing the change"},
            },
            "required": ["session_id", "message"],
        },
    },
]

# Map tool_set values (stored in the agents DB table) to their tool lists.
_TOOL_SETS: dict[str, list] = {
    "default": _DEFAULT_AGENT_TOOLS,
    "content_writer": _CONTENT_TOOLS,
    "website": _WEBSITE_TOOLS,
}


# ── Tool execution ────────────────────────────────────────────────────────────

def _execute_tool(name: str, inp: dict) -> str:
    """Synchronous tool dispatch — shared by router, specialists, and scheduled reports.

    Handles all tools that don't need async context (bot, chat_id).
    Async tools (delegate_to_agent, send_approved_email, send_post_image)
    are handled inline by _handle_user_message.
    """
    # ── Read tools ────────────────────────────────────────────────────────
    if name == "get_all_tasks":
        return tasks_to_json()
    if name == "get_all_posts":
        return posts_to_json()
    if name == "get_post_by_date":
        post = db.get_post_meta_by_date(inp["date"])
        return json.dumps(dict(post), default=str) if post else f"No post found for {inp['date']}."
    if name == "get_brand":
        return db.get_brand_guidelines()
    if name == "get_contact_by_name":
        contact = db.get_contact_by_name(inp["name"])
        return json.dumps(dict(contact), default=str) if contact else f"No contact found matching '{inp['name']}'."
    if name == "get_email_history":
        history = db.get_email_history_for_contact(inp["contact_id"])
        return json.dumps([dict(r) for r in history], default=str) if history else "No emails found."

    # ── Write tools ───────────────────────────────────────────────────────
    if name == "update_task_status":
        db.update_task_status_by_name(inp["name"], inp["status"])
        return f"Status of {inp['name']} updated to {inp['status']}."
    if name == "update_brand":
        db.update_brand_guidelines(inp["content"])
        return "Brand guidelines updated."
    if name == "create_post":
        new_channels = set((inp.get("channels") or "instagram").split(","))
        existing = db.get_posts_for_date(inp["date"])
        conflicts = [
            f"id={p['id']} channels={p['channels']}"
            for p in existing
            if new_channels & set((p.get("channels") or "instagram").split(","))
        ]
        try:
            post_id = db.create_post(**inp)
            msg = f"Post created (id={post_id}, date={inp['date']}, format={inp['format']})."
            if conflicts:
                msg += f" Warning: existing post(s) for this date share the same channel: {', '.join(conflicts)}."
            msg += " Saved as draft — review and approve in the portal."
            return msg
        except Exception as e:
            return f"Error creating post: {e}"
    if name == "update_post":
        post_id = inp.pop("id")
        post = db.get_post(post_id)
        if not post:
            return f"No post found with id={post_id}."
        warnings = []
        if "caption" in inp and post.get("caption_locked"):
            warnings.append("caption is locked (manually edited in portal) — skipped")
            inp.pop("caption")
        if "image_prompt" in inp and post.get("image_locked"):
            warnings.append("image_prompt is locked (image manually uploaded in portal) — skipped")
            inp.pop("image_prompt")
        if inp:
            db.update_post_fields(post_id, **inp)
        msg = f"Post {post_id} updated." if inp else f"Post {post_id}: no fields updated."
        if warnings:
            msg += f" Warning: {'; '.join(warnings)}."
        return msg

    # ── Agent management ──────────────────────────────────────────────────
    if name == "create_agent":
        agent_name_slug = inp["name"].strip().lower().replace(" ", "_")
        if db.get_agent_by_name(agent_name_slug):
            return f"An agent named '{agent_name_slug}' already exists. Use update_agent to modify it."
        tool_set = inp.get("tool_set", "default")
        db.upsert_agent(agent_name_slug, inp["description"], inp["system_prompt"], inp["model"], is_router=False, tool_set=tool_set)
        return f"Agent '{agent_name_slug}' created successfully."
    if name == "update_agent":
        try:
            db.update_agent_field_by_name(inp["name"], inp["field"], inp["new_value"])
            return f"Agent '{inp['name']}' — {inp['field']} updated."
        except ValueError as e:
            return str(e)
    if name == "delete_agent":
        db.delete_agent_by_name(inp["name"])
        return f"Agent '{inp['name']}' deleted."

    # ── Project management ────────────────────────────────────────────────
    if name == "create_project":
        try:
            project = db.create_project(
                inp["name"], inp.get("description"), inp.get("status", "planning"), inp.get("deadline"),
            )
            return f"Project '{project['name']}' created (status: {project['status']})."
        except Exception as e:
            return f"Could not create project: {e}"
    if name == "rename_project":
        project = db.get_project_by_name(inp["name"])
        if not project:
            return f"No project named '{inp['name']}' found."
        db.update_project(project["id"], name=inp["new_name"])
        return f"Project renamed to '{inp['new_name']}'."
    if name == "delete_project":
        project = db.get_project_by_name(inp["name"])
        if not project:
            return f"No project named '{inp['name']}' found."
        db.delete_project(project["id"])
        return f"Project '{inp['name']}' deleted. Its tasks are now unassigned."
    if name == "assign_task_to_project":
        task_row = None
        for t in db.get_all_tasks():
            if t["name"].lower() == inp["task_name"].lower():
                task_row = t
                break
        if not task_row:
            return f"No task named '{inp['task_name']}' found."
        if not inp.get("project_name"):
            db.update_task_fields(task_row["id"], project_id=None)
            return f"Task '{task_row['name']}' unassigned from its project."
        project = db.get_project_by_name(inp["project_name"])
        if not project:
            return f"No project named '{inp['project_name']}' found."
        db.update_task_fields(task_row["id"], project_id=project["id"])
        return f"Task '{task_row['name']}' assigned to project '{project['name']}'."
    if name == "assign_post_to_project":
        post_row = db.get_post_meta_by_date(inp["date"])
        if not post_row:
            return f"No post found for date '{inp['date']}'."
        if not inp.get("project_name"):
            db.set_post_project(post_row["id"], None)
            return f"Post {inp['date']} unassigned from its project."
        project = db.get_project_by_name(inp["project_name"])
        if not project:
            return f"No project named '{inp['project_name']}' found."
        db.set_post_project(post_row["id"], project["id"])
        return f"Post {inp['date']} assigned to project '{project['name']}'."

    # ── Website editing tools ─────────────────────────────────────────────
    if name == "website_list_files":
        from services import github as gh
        try:
            tmpdir = gh.clone()
            files = gh.list_files(tmpdir)
            return json.dumps({"session_id": tmpdir, "files": files})
        except Exception as e:
            return f"Error: {e}"

    if name == "website_read_file":
        from services import github as gh
        return gh.read_file(inp["session_id"], inp["path"])

    if name == "website_edit_file":
        from services import github as gh
        old = gh.read_file(inp["session_id"], inp["path"])
        gh.write_file(inp["session_id"], inp["path"], inp["content"])
        diff = gh.text_diff(old, inp["content"], inp["path"])
        return f"File updated.\n\nText changes:\n{diff}"

    if name == "website_commit":
        from services import github as gh
        try:
            result = gh.commit_and_push(inp["session_id"], inp["message"])
        except Exception as e:
            result = f"Git error: {e}"
        finally:
            gh.cleanup(inp["session_id"])
        return result

    # ── Async-only tools (not available in all contexts) ──────────────────
    if name in ("send_approved_email", "send_post_image", "delegate_to_agent"):
        return f"Tool '{name}' is not available in this context."

    return f"Unknown tool: {name}"


# ── Specialist agent call (one-shot, no persistent history) ───────────────────

async def _call_specialist(agent_name: str, message: str, agent_language: str = "en") -> str:
    """Call a specialist agent with its tools. Returns the final text response."""
    agent = db.get_agent_by_name(agent_name)
    if not agent:
        return f"No agent named '{agent_name}' found."

    system = build_system_prompt(agent_name, extra=f"AGENT_LANGUAGE: {agent_language}")
    tools = _TOOL_SETS.get(agent.get("tool_set", "default"), _DEFAULT_AGENT_TOOLS)
    messages = [{"role": "user", "content": message}]

    while True:
        response = await call_claude(
            model=agent["model"],
            max_tokens=2048,
            system=system,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return next((b.text for b in response.content if hasattr(b, "text")), "Done.")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, dict(block.input))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


# ── Router loop ──────────────────────────────────────────────────────────────

async def _send_approved_email(inp: dict) -> str:
    """Send an email (new or threaded reply) via the multi-provider dispatcher."""
    from services.email import send_email_to_contact, send_reply_to_contact

    reply_inbox_id = inp.get("reply_to_inbox_id")
    msg_id = None

    if reply_inbox_id:
        inbox_msg = db.get_inbox_message(reply_inbox_id)
        if not (inbox_msg and inbox_msg["thread_id"]):
            return "Could not find the original email to reply to."
        msg_id = await asyncio.to_thread(
            send_reply_to_contact,
            inp["to_email"],
            inp.get("to_name", ""),
            inbox_msg["subject"],
            inp["body"],
            inbox_msg["thread_id"],
            inbox_msg["provider_message_id"],
            inbox_msg.get("provider", "gmail"),
            inp.get("summary", ""),
            inp.get("contact_id"),
            inp.get("task_id"),
        )
    else:
        msg_id = await asyncio.to_thread(
            send_email_to_contact,
            inp["to_email"],
            inp.get("to_name", ""),
            inp["subject"],
            inp["body"],
            inp.get("summary", ""),
            inp.get("contact_id"),
            inp.get("task_id"),
        )

    if not msg_id:
        return "Failed to send email — no connected email account or credential error."
    if msg_id.startswith("outlook:"):
        return f"Email sent to {inp['to_email']} via Outlook."
    gmail_link = f"https://mail.google.com/mail/u/0/#sent/{msg_id}"
    return f"Email sent to {inp['to_email']}. View in Gmail: {gmail_link}"


async def _send_post_image(date_str: str, chat_id: int, bot: Bot) -> str:
    """Fetch a post's image(s) from DB and send to Telegram. Returns status string."""
    post = db.get_post_meta_by_date(date_str)
    if not post:
        return f"No post found for {date_str}."
    full = db.get_post(post["id"])
    if not full:
        return f"No post data found for {date_str}."
    caption = f"{post['date']} — {post['theme']}"
    try:
        await send_post_media(full, chat_id, bot, caption)
        return "Image sent." if post["format"] == "static" else "Images sent."
    except ValueError as e:
        return str(e)


async def _handle_user_message(user_text: str, chat_id: int, bot: Bot | None = None) -> str:
    """Route all messages through the router agent."""
    router = db.get_router_agent()
    if not router:
        return "No router agent configured. Please set one up in the Team settings in the portal."

    agent_language = db.get_setting("agent_language", "en")
    owner_name = db.get_setting("owner_name", "the user")
    system = build_system_prompt(
        router["name"],
        extra=f"AGENT_LANGUAGE: {agent_language}\nCURRENT TASKS:\n{tasks_to_json()}",
    )

    _add_to_history(chat_id, "user", user_text)
    messages = list(_get_history(chat_id))

    while True:
        response = await call_claude(
            model=router["model"],
            max_tokens=2048,
            system=system,
            tools=_build_router_tools(owner_name),
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            reply = next((b.text for b in response.content if hasattr(b, "text")), "...")
            _add_to_history(chat_id, "assistant", reply)
            return reply

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            name, inp = block.name, dict(block.input)

            # Async tools that need bot/chat_id context
            if name == "delegate_to_agent":
                result = await _call_specialist(inp["agent_name"], inp["message"], agent_language)
            elif name == "send_post_image":
                if bot:
                    result = await _send_post_image(inp["date"], chat_id, bot)
                else:
                    result = "Cannot send image in this context."
            elif name == "send_approved_email":
                result = await _send_approved_email(inp)
            else:
                result = _execute_tool(name, inp)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]


# ── Scheduled jobs ────────────────────────────────────────────────────────────

def _extract_report_section(system_prompt: str, heading: str) -> str | None:
    """Return the body of a ## heading section, or None if the section is absent."""
    pattern = rf"##\s+{_re.escape(heading)}\s*\n([\s\S]*?)(?=\n##\s|\Z)"
    match = _re.search(pattern, system_prompt)
    if match:
        body = match.group(1).strip()
        return body if body else None
    return None


async def _run_report_job(bot: Bot, section_heading: str, label: str) -> None:
    """Run a scheduled report if the router's system_prompt has the named section."""
    router = db.get_router_agent()
    if not router:
        logger.info("Scheduled report '%s' skipped — no router agent", label)
        return

    section_body = _extract_report_section(router["system_prompt"], section_heading)
    if not section_body:
        logger.info("Scheduled report '%s' skipped — no '%s' section in router prompt", label, section_heading)
        return

    today = date.today().strftime("%Y-%m-%d")
    prompt = f"Today is {today}.\n\n{section_body}"

    agent_language = db.get_setting("agent_language", "en")
    owner_name = db.get_setting("owner_name", "the user")
    system = build_system_prompt(
        router["name"],
        extra=f"AGENT_LANGUAGE: {agent_language}\nCURRENT TASKS:\n{tasks_to_json()}",
    )

    # Full agentic loop — same tool execution as _handle_user_message but
    # with a fresh message list so conversation history is not polluted.
    messages = [{"role": "user", "content": prompt}]
    try:
        while True:
            response = await call_claude(
                model=router["model"],
                max_tokens=2048,
                system=system,
                tools=_build_router_tools(owner_name),
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                reply = next((b.text for b in response.content if hasattr(b, "text")), None)
                if reply:
                    for user_id in _allowed_user_ids():
                        await bot.send_message(chat_id=user_id, text=f"{label}\n\n{reply}")
                    logger.info("%s sent", label)
                return

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                name, inp = block.name, dict(block.input)
                if name == "delegate_to_agent":
                    result = await _call_specialist(inp["agent_name"], inp["message"], agent_language)
                else:
                    result = _execute_tool(name, inp)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
    except Exception as e:
        logger.error("%s failed: %s", label, e)


async def job_daily(bot: Bot) -> None:
    today = date.today().strftime("%Y-%m-%d")
    await _run_report_job(bot, "Daily Report", f"Daily Check — {today}")


async def _summarize_email(subject: str, body: str, agent_language: str) -> tuple[str, str]:
    """Return (subject_translated, summary) in agent_language."""
    prompt = (
        f"You received this email. Translate the subject and write a 1–2 sentence summary "
        f"of the body. Respond in this language: {agent_language}.\n\n"
        f"Subject: {subject}\n\nBody:\n{body[:2000]}\n\n"
        f"Reply in this exact format (no extra text):\n"
        f"SUBJECT: <translated subject>\nSUMMARY: <1-2 sentence summary>"
    )
    response = await call_claude(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    subject_translated = subject
    summary = ""
    for line in text.splitlines():
        if line.startswith("SUBJECT:"):
            subject_translated = line[len("SUBJECT:"):].strip()
        elif line.startswith("SUMMARY:"):
            summary = line[len("SUMMARY:"):].strip()
    return subject_translated, summary


async def job_gmail(bot: Bot) -> None:
    from services.email import fetch_all_inboxes
    try:
        new_matches = await asyncio.to_thread(fetch_all_inboxes, max_results=50)
    except Exception as e:
        logger.error("Email inbox poll failed: %s", e)
        return
    if not new_matches:
        return

    agent_language = db.get_setting("agent_language", "en")
    lines = [f"📬 New email replies ({len(new_matches)}):"]
    for m in new_matches:
        try:
            subject_translated, summary = await _summarize_email(
                m["subject"] or "", m.get("body") or "", agent_language
            )
            db.update_inbox_summary(m["id"], subject_translated, summary)
        except Exception as e:
            logger.error("Summarize email failed for inbox id %s: %s", m.get("id"), e)
            subject_translated = m["subject"] or "(no subject)"
            summary = ""
        task_info = f" → task #{m['task_id']}" if m.get("task_id") else ""
        lines.append(f"• {m['contact_name']}{task_info}: {subject_translated}")
        if summary:
            lines.append(f"  {summary}")

    message = "\n".join(lines)
    for user_id in _allowed_user_ids():
        await bot.send_message(chat_id=user_id, text=message)
    logger.info("Gmail poll: %d new matches notified", len(new_matches))


async def job_weekly(bot: Bot) -> None:
    today = date.today().strftime("%Y-%m-%d")
    await _run_report_job(bot, "Weekly Report", f"Weekly Agenda — {today}")


# ── Telegram handlers ─────────────────────────────────────────────────────────

def _is_allowed(user_id: int) -> bool:
    return user_id in _allowed_user_ids()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    router = db.get_router_agent()
    router_name = router["name"].capitalize() if router else "Assistant"
    await update.message.reply_text(
        f"Hello! I'm {router_name}, your central assistant.\n\n"
        "Just send me a message and I'll take it from there.\n\n"
        "Commands:\n"
        "/reset — Clear conversation history\n"
        "/help — Show this message"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    conversation_history.pop(update.effective_chat.id, None)
    await update.message.reply_text("Conversation history cleared.")


async def _transcribe_voice(voice_bytes: bytes) -> str:
    """Transcribe OGG/OPUS voice bytes using Gemini."""
    from google import genai
    from google.genai import types as gtypes
    key = db.get_setting("google_ai_studio_key", "")
    if not key:
        raise ValueError("Google AI Studio key not configured. Set it in portal Settings.")
    gc = genai.Client(api_key=key)
    response = await asyncio.to_thread(
        gc.models.generate_content,
        model="gemini-2.5-flash",
        contents=[
            gtypes.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg"),
            "Transcribe this voice message accurately. Return only the transcription, nothing else.",
        ],
    )
    return response.text.strip()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    user_text = update.message.text or ""
    if not user_text.strip():
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = await _handle_user_message(user_text, update.effective_chat.id, bot=context.bot)
        for chunk in [reply[i: i + 4000] for i in range(0, len(reply), 4000)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.error("Message handler error: %s", e)
        await update.message.reply_text(f"Something went wrong: {e}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_bytes = await voice_file.download_as_bytearray()
        user_text = await _transcribe_voice(bytes(voice_bytes))
        if not user_text:
            await update.message.reply_text("Sorry, I couldn't transcribe that.")
            return
        logger.info("Voice transcribed: %s", user_text[:80])
        await update.message.reply_text(f"🎤 {user_text}")
        reply = await _handle_user_message(user_text, update.effective_chat.id, bot=context.bot)
        for chunk in [reply[i: i + 4000] for i in range(0, len(reply), 4000)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.error("Voice handler error: %s", e)
        await update.message.reply_text(f"Something went wrong with the voice message: {e}")


def create_application() -> Application:
    """Build and return the Telegram Application with all handlers registered."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    return app
