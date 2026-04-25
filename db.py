"""PostgreSQL database layer — shared by bot, content job, and portal."""

import json
import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent

CLAUDE_MODELS = ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"]


def _dsn() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("postgres://", "postgresql://", 1)


@contextmanager
def get_conn():
    conn = psycopg2.connect(_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables, migrate existing tables, then seed from files."""
    with get_conn() as conn:
        cur = conn.cursor()

        # ── Migrate: rename old contacts table → tasks ────────────────────────
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables WHERE table_name = 'contacts'
            ) AS contacts_exists,
            EXISTS (
                SELECT FROM information_schema.tables WHERE table_name = 'tasks'
            ) AS tasks_exists
        """)
        row = cur.fetchone()
        contacts_exists, tasks_exists = row[0], row[1]
        if contacts_exists and not tasks_exists:
            cur.execute("ALTER TABLE contacts RENAME TO tasks")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                handle TEXT,
                role TEXT,
                contact TEXT,
                channel TEXT,
                priority TEXT,
                status TEXT DEFAULT 'not_started',
                deadline DATE,
                notes TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Migrate tasks table — add new columns
        for col, definition in [
            ("contact_id",     "INTEGER"),
            ("email_subject",  "TEXT"),
            ("email_body",     "TEXT"),
            ("email_status",   "TEXT DEFAULT 'no_draft'"),
            ("sent_at",        "TIMESTAMP"),
            ("created_at",     "TIMESTAMP DEFAULT NOW()"),
        ]:
            cur.execute(f"""
                ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {col} {definition}
            """)

        # ── New contacts table (address book) ─────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                first_name TEXT,
                company TEXT,
                email TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Migrate contacts — add correspondence_language if missing
        cur.execute("""
            ALTER TABLE contacts ADD COLUMN IF NOT EXISTS correspondence_language TEXT
        """)

        # Now that contacts table exists, add FK constraint on tasks.contact_id if missing
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'tasks_contact_id_fkey'
                ) THEN
                    ALTER TABLE tasks
                        ADD CONSTRAINT tasks_contact_id_fkey
                        FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL;
                END IF;
            END $$
        """)

        # ── Email accounts (multi-account, multi-provider) ───────────────────
        # Migrate: rename gmail_accounts → email_accounts
        cur.execute("""
            DO $$
            BEGIN
              IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'gmail_accounts')
                 AND NOT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'email_accounts')
              THEN
                ALTER TABLE gmail_accounts RENAME TO email_accounts;
              END IF;
            END $$
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_accounts (
                id SERIAL PRIMARY KEY,
                label TEXT,
                email TEXT,
                client_id TEXT,
                client_secret TEXT,
                callback_url TEXT,
                access_token TEXT,
                refresh_token TEXT,
                token_expiry TIMESTAMP,
                provider TEXT DEFAULT 'gmail',
                is_default BOOLEAN DEFAULT FALSE,
                tenant_id TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        for col, definition in [
            ("label",       "TEXT"),
            ("callback_url","TEXT"),
            ("provider",    "TEXT DEFAULT 'gmail'"),
            ("is_default",  "BOOLEAN DEFAULT FALSE"),
            ("tenant_id",   "TEXT"),
        ]:
            cur.execute(f"ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS {col} {definition}")

        # Migrate contacts: add preferred sending account
        cur.execute("""
            ALTER TABLE contacts ADD COLUMN IF NOT EXISTS preferred_email_account_id INTEGER
        """)
        cur.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'contacts_preferred_email_account_id_fkey'
              ) THEN
                ALTER TABLE contacts ADD CONSTRAINT contacts_preferred_email_account_id_fkey
                  FOREIGN KEY (preferred_email_account_id) REFERENCES email_accounts(id) ON DELETE SET NULL;
              END IF;
            END $$
        """)

        # ── Email inbox ───────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_inbox (
                id SERIAL PRIMARY KEY,
                provider_message_id TEXT UNIQUE,
                provider TEXT DEFAULT 'gmail',
                thread_id TEXT,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                from_email TEXT,
                subject TEXT,
                subject_translated TEXT,
                body TEXT,
                summary TEXT,
                received_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE email_inbox ADD COLUMN IF NOT EXISTS subject_translated TEXT")
        cur.execute("ALTER TABLE email_inbox ADD COLUMN IF NOT EXISTS summary TEXT")
        cur.execute("ALTER TABLE email_inbox ADD COLUMN IF NOT EXISTS thread_id TEXT")

        # Migrate: rename gmail_message_id → provider_message_id in email_inbox
        cur.execute("""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_name = 'email_inbox' AND column_name = 'gmail_message_id'
              ) THEN
                ALTER TABLE email_inbox RENAME COLUMN gmail_message_id TO provider_message_id;
              END IF;
            END $$
        """)
        cur.execute("ALTER TABLE email_inbox ADD COLUMN IF NOT EXISTS provider TEXT DEFAULT 'gmail'")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL UNIQUE,
                format TEXT NOT NULL,
                pillar TEXT,
                theme TEXT,
                image_prompt TEXT,
                image_style TEXT,
                n_slides INTEGER,
                caption TEXT,
                image_bytes BYTEA,
                image_mime_type TEXT DEFAULT 'image/jpeg',
                image_style_type TEXT DEFAULT 'realistic',
                image_model_used TEXT,
                image_prompt_sent TEXT,
                approved BOOLEAN DEFAULT FALSE,
                posted_at TIMESTAMP,
                generated_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Migrate: copy linkedin_caption → caption where caption is empty (if column still exists)
        cur.execute("""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'posts' AND column_name = 'linkedin_caption'
              ) THEN
                UPDATE posts
                SET caption = linkedin_caption
                WHERE linkedin_caption IS NOT NULL
                  AND linkedin_caption != ''
                  AND (caption IS NULL OR caption = '');
              END IF;
            END $$
        """)

        # Migrate: drop linkedin_caption column (now unified into caption)
        cur.execute("""
            ALTER TABLE posts DROP COLUMN IF EXISTS linkedin_caption
        """)

        # Migrate: drop UNIQUE constraint on posts.date (allow multiple posts per date)
        cur.execute("""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_name = 'posts' AND constraint_name = 'posts_date_key'
              ) THEN
                ALTER TABLE posts DROP CONSTRAINT posts_date_key;
              END IF;
            END $$
        """)

        # Migrate existing posts table — add new columns if they don't exist
        for col, definition in [
            ("image_style_type",    "TEXT DEFAULT 'realistic'"),
            ("image_model_used",    "TEXT"),
            ("image_prompt_sent",   "TEXT"),
            ("video_prompt",        "TEXT"),
            ("image_locked",        "BOOLEAN DEFAULT FALSE"),
            ("caption_locked",      "BOOLEAN DEFAULT FALSE"),
            ("video_bytes",         "BYTEA"),
            ("video_mime_type",     "TEXT"),
            ("video_generated_at",  "TIMESTAMP"),
            ("channels",            "TEXT DEFAULT 'instagram'"),
            ("linkedin_title",      "TEXT"),
            ("linkedin_article_body", "TEXT"),
            ("linkedin_posted_at",  "TIMESTAMP"),
        ]:
            cur.execute(f"""
                ALTER TABLE posts ADD COLUMN IF NOT EXISTS {col} {definition}
            """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS slides (
                id SERIAL PRIMARY KEY,
                post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
                slide_number INTEGER NOT NULL,
                title_en TEXT,
                content TEXT,
                image_bytes BYTEA,
                image_mime_type TEXT DEFAULT 'image/jpeg',
                UNIQUE(post_id, slide_number)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                system_prompt TEXT NOT NULL,
                model TEXT DEFAULT 'claude-sonnet-4-6',
                is_router BOOLEAN DEFAULT FALSE,
                tool_set TEXT DEFAULT 'default',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS brand_guidelines (
                id INTEGER PRIMARY KEY DEFAULT 1,
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # ── Settings (global key/value store) ─────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cur.execute("""
            INSERT INTO settings (key, value) VALUES ('agent_language', 'en')
            ON CONFLICT (key) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO settings (key, value) VALUES ('enabled_channels', 'instagram')
            ON CONFLICT (key) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO settings (key, value)
            VALUES ('gemini_image_model', 'gemini-3.1-flash-image-preview,gemini-2.5-flash-image')
            ON CONFLICT (key) DO NOTHING
        """)

        # ── LinkedIn accounts ─────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_accounts (
                id SERIAL PRIMARY KEY,
                label TEXT,
                client_id TEXT,
                client_secret TEXT,
                callback_url TEXT,
                access_token TEXT,
                refresh_token TEXT,
                token_expiry TIMESTAMP,
                linkedin_id TEXT,
                person_name TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # ── Email log (sent emails history) ───────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_log (
                id SERIAL PRIMARY KEY,
                to_name TEXT,
                to_email TEXT NOT NULL,
                subject TEXT,
                summary TEXT,
                sent_at TIMESTAMP DEFAULT NOW(),
                provider_message_id TEXT,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                email_account_id INTEGER REFERENCES email_accounts(id) ON DELETE SET NULL
            )
        """)

        # Migrate: rename gmail_message_id → provider_message_id in email_log
        cur.execute("""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_name = 'email_log' AND column_name = 'gmail_message_id'
              ) THEN
                ALTER TABLE email_log RENAME COLUMN gmail_message_id TO provider_message_id;
              END IF;
            END $$
        """)
        cur.execute("ALTER TABLE email_log ADD COLUMN IF NOT EXISTS email_account_id INTEGER")
        cur.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'email_log_email_account_id_fkey'
              ) THEN
                ALTER TABLE email_log ADD CONSTRAINT email_log_email_account_id_fkey
                  FOREIGN KEY (email_account_id) REFERENCES email_accounts(id) ON DELETE SET NULL;
              END IF;
            END $$
        """)

        # ── Projects ──────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                status TEXT DEFAULT 'planning',
                deadline DATE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id INTEGER")
        cur.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'tasks_project_id_fkey'
              ) THEN
                ALTER TABLE tasks ADD CONSTRAINT tasks_project_id_fkey
                  FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;
              END IF;
            END $$
        """)
        cur.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS project_id INTEGER")
        cur.execute("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'posts_project_id_fkey'
              ) THEN
                ALTER TABLE posts ADD CONSTRAINT posts_project_id_fkey
                  FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;
              END IF;
            END $$
        """)
        cur.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tool_set TEXT DEFAULT 'default'")

    _seed_agents()
    _seed_brand()


def _seed_tasks():
    path = BASE_DIR / "data" / "contacts.json"
    if not path.exists():
        return
    raw = json.loads(path.read_text())
    tasks = raw.get("contacts", raw) if isinstance(raw, dict) else raw
    with get_conn() as conn:
        cur = conn.cursor()
        for c in tasks:
            cur.execute("""
                INSERT INTO tasks (name, handle, role, contact, channel, priority, status, deadline, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
            """, (
                c.get("name"), c.get("handle"), c.get("role"),
                c.get("contact"), c.get("channel"),
                c.get("priority", "MEDIUM"),
                c.get("status", "not_started"),
                c.get("deadline") or None,
                c.get("notes"),
            ))


_ROUTER_SYSTEM_PROMPT = """\
# Assistant — Central Coordinator

You are the central assistant and coordinator for this team. You are the single point of contact. \
Every message comes to you first. You decide whether to handle it yourself or delegate to a specialist.

## What You Handle Directly

- Contact tracking — status, deadlines, overdue, next actions
- Scheduling — weekly agenda, reminders, timeline
- Brand guidelines — reading and updating
- Email drafting and sending (with user approval)
- Content calendar — reading posts by date
- Team management — creating, updating, and deleting specialist agents

## Delegation

When specialist agents are listed in AVAILABLE AGENTS in your context, delegate tasks to them \
using the **delegate_to_agent** tool. Always include the full conversation context so the specialist \
has everything they need. When a specialist replies, introduce them naturally:
- "Let me check with [Name]... [response]"
- "I've asked [Name] — here's what they say: [response]"

## Managing Team Members

- **create_agent** — Add a new specialist. If name or model not provided, ask. \
Suggest claude-sonnet-4-6 as default model. Generate a complete system_prompt from the user's \
description, informed by the brand context. Show the draft prompt for confirmation before saving.
- **update_agent** — Change an agent's description, system_prompt, or model. Show the change before applying.
- **delete_agent** — Remove a specialist. Always confirm explicitly before deleting. \
Never attempt to delete the router/assistant itself.

## Post Creation Workflow

When the user wants to create a new post, delegate to the appropriate specialist. They will gather \
all required fields and save the post as a draft. The user reviews and approves in the portal.

## Brand Guidelines

- Use `get_brand` to read current guidelines
- Use `update_brand` only when explicitly asked — show the change first

## Contact Book

You have access to all contacts. Use this to answer deadline and status questions.

## Email Flow

1. Call `get_contact_by_name` — get email and correspondence_language
2. Draft: delegate to specialist if outreach/partnership; draft yourself if operational
3. Show for approval in AGENT_LANGUAGE: summary, full body, To/Subject, "Shall I send this?"
4. Wait for explicit approval before calling `send_approved_email`

## Rules

- Never fabricate contact details or post information — always read from the database
- Never send email without explicit approval
- Keep responses short and actionable
- Respond in the language specified by AGENT_LANGUAGE
"""


def _seed_agents():
    """Seed a generic router agent only if no router exists yet."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM agents WHERE is_router = TRUE")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO agents (name, description, system_prompt, model, is_router)
                VALUES ('assistant', 'Central coordinator and team manager', %s, 'claude-sonnet-4-6', TRUE)
            """, (_ROUTER_SYSTEM_PROMPT,))


def _seed_brand():
    path = BASE_DIR / "context" / "brand.md"
    if not path.exists():
        return
    content = path.read_text()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO brand_guidelines (id, content)
            VALUES (1, %s)
            ON CONFLICT (id) DO NOTHING
        """, (content,))


# ── Tasks (outreach pipeline, formerly "contacts") ────────────────────────────

def get_all_tasks():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.*, c.name AS contact_name, c.email AS contact_email,
                   p.name AS project_name
            FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            LEFT JOIN projects p ON p.id = t.project_id
            ORDER BY
                CASE t.priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1
                                WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                t.deadline NULLS LAST, t.name
        """)
        return cur.fetchall()


def get_task(task_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.*, c.name AS contact_name, c.first_name AS contact_first_name,
                   c.company AS contact_company, c.email AS contact_email,
                   p.name AS project_name
            FROM tasks t
            LEFT JOIN contacts c ON c.id = t.contact_id
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.id = %s
        """, (task_id,))
        return cur.fetchone()


def update_task_status(task_id: int, status: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET status = %s, updated_at = NOW() WHERE id = %s",
            (status, task_id),
        )


def update_task_status_by_name(name: str, status: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET status = %s, updated_at = NOW() WHERE LOWER(name) = LOWER(%s)",
            (status, name),
        )


def update_task_fields(task_id: int, **fields):
    allowed = {"contact_id", "project_id", "email_subject", "email_body", "email_status",
               "sent_at", "status", "priority", "deadline", "notes", "name"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    set_clause += ", updated_at = NOW()"
    values = list(updates.values()) + [task_id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE tasks SET {set_clause} WHERE id = %s", values)


# ── Projects ─────────────────────────────────────────────────────────────────

PROJECT_STATUSES = ["planning", "active", "completed", "archived"]


def get_all_projects():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.*,
                   COUNT(DISTINCT t.id) AS task_count,
                   COUNT(DISTINCT t.id) FILTER (WHERE t.status NOT IN ('confirmed', 'declined')) AS open_task_count,
                   COUNT(DISTINCT po.id) AS post_count
            FROM projects p
            LEFT JOIN tasks t ON t.project_id = p.id
            LEFT JOIN posts po ON po.project_id = p.id
            GROUP BY p.id
            ORDER BY p.deadline NULLS LAST, p.name
        """)
        return cur.fetchall()


def get_project(project_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        project = cur.fetchone()
        if not project:
            return None, [], []
        cur.execute("""
            SELECT t.id, t.name, t.status, t.priority, t.deadline, t.updated_at
            FROM tasks t
            WHERE t.project_id = %s
            ORDER BY
                CASE t.priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1
                                WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                t.deadline NULLS LAST, t.name
        """, (project_id,))
        tasks = cur.fetchall()
        cur.execute("""
            SELECT id, date, format, pillar, theme, approved, posted_at
            FROM posts
            WHERE project_id = %s
            ORDER BY date
        """, (project_id,))
        posts = cur.fetchall()
        return project, tasks, posts


def get_project_by_name(name: str):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM projects WHERE LOWER(name) = LOWER(%s)", (name,))
        return cur.fetchone()


def create_project(name: str, description: str = None, status: str = "planning", deadline=None):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO projects (name, description, status, deadline)
            VALUES (%s, %s, %s, %s)
            RETURNING *
        """, (name, description, status, deadline))
        return cur.fetchone()


def update_project(project_id: int, **fields):
    allowed = {"name", "description", "status", "deadline"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    set_clause += ", updated_at = NOW()"
    values = list(updates.values()) + [project_id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE projects SET {set_clause} WHERE id = %s", values)


def delete_project(project_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))


# ── Contacts (address book) ───────────────────────────────────────────────────

def get_all_contacts():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM contacts ORDER BY name, first_name")
        return cur.fetchall()


def get_contact(contact_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,))
        return cur.fetchone()


def get_contact_by_email(email: str):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM contacts WHERE LOWER(email) = LOWER(%s)", (email,))
        return cur.fetchone()


def get_contact_by_name(name: str):
    """Fuzzy search contacts by name, first_name, or company. Returns best match."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM contacts
            WHERE LOWER(name) LIKE LOWER(%s)
               OR LOWER(first_name) LIKE LOWER(%s)
               OR LOWER(company) LIKE LOWER(%s)
            ORDER BY name LIMIT 1
        """, (f"%{name}%", f"%{name}%", f"%{name}%"))
        return cur.fetchone()


def create_contact(name: str, first_name: str, company: str, email: str,
                   correspondence_language: str = None) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contacts (name, first_name, company, email, correspondence_language)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (name, first_name or None, company or None, email or None,
              correspondence_language or None))
        return cur.fetchone()[0]


def update_contact(contact_id: int, name: str, first_name: str, company: str, email: str,
                   correspondence_language: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE contacts SET name = %s, first_name = %s, company = %s, email = %s,
                               correspondence_language = %s
            WHERE id = %s
        """, (name, first_name or None, company or None, email or None,
              correspondence_language or None, contact_id))


def delete_contact(contact_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))


# ── Email accounts (multi-account, multi-provider) ────────────────────────────

def get_all_email_accounts():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM email_accounts ORDER BY is_default DESC, id")
        return cur.fetchall()


def get_connected_email_accounts():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM email_accounts WHERE access_token IS NOT NULL ORDER BY is_default DESC, id"
        )
        return cur.fetchall()


def get_connected_gmail_accounts():
    """Backward-compat: return only Gmail accounts with tokens."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM email_accounts WHERE provider = 'gmail' AND access_token IS NOT NULL ORDER BY id"
        )
        return cur.fetchall()


def get_email_account(account_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM email_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()


def create_email_account(label: str, client_id: str, client_secret: str,
                         provider: str = "gmail", callback_url: str = None,
                         tenant_id: str = None) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO email_accounts (label, client_id, client_secret, callback_url,
                                        provider, tenant_id, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (label or None, client_id or None, client_secret or None,
              callback_url or None, provider, tenant_id or None))
        return cur.fetchone()[0]


def save_email_config(account_id: int, label: str, client_id: str, client_secret: str,
                      callback_url: str = None, tenant_id: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE email_accounts
            SET label = %s, client_id = %s, client_secret = %s,
                callback_url = %s, tenant_id = %s, updated_at = NOW()
            WHERE id = %s
        """, (label or None, client_id or None, client_secret or None,
              callback_url or None, tenant_id or None, account_id))


def save_email_tokens(account_id: int, email: str, access_token: str,
                      refresh_token: str, token_expiry):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE email_accounts
            SET email = %s, access_token = %s, refresh_token = %s,
                token_expiry = %s, updated_at = NOW()
            WHERE id = %s
        """, (email, access_token, refresh_token, token_expiry, account_id))


def update_email_access_token(account_id: int, access_token: str, token_expiry):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE email_accounts
            SET access_token = %s, token_expiry = %s, updated_at = NOW()
            WHERE id = %s
        """, (access_token, token_expiry, account_id))


def clear_email_tokens(account_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE email_accounts
            SET access_token = NULL, refresh_token = NULL,
                token_expiry = NULL, email = NULL, updated_at = NOW()
            WHERE id = %s
        """, (account_id,))


def delete_email_account(account_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM email_accounts WHERE id = %s", (account_id,))


def set_email_account_default(account_id: int):
    """Make one account the global default, clear is_default on all others."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE email_accounts SET is_default = FALSE")
        cur.execute("UPDATE email_accounts SET is_default = TRUE WHERE id = %s", (account_id,))


def get_account_for_contact(contact_id: int | None):
    """Resolve sending account: preferred → default → first connected."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if contact_id:
            cur.execute("""
                SELECT ea.* FROM email_accounts ea
                JOIN contacts c ON c.preferred_email_account_id = ea.id
                WHERE c.id = %s AND ea.access_token IS NOT NULL
            """, (contact_id,))
            row = cur.fetchone()
            if row:
                return row
        cur.execute("""
            SELECT * FROM email_accounts
            WHERE access_token IS NOT NULL
            ORDER BY is_default DESC, id ASC
            LIMIT 1
        """)
        return cur.fetchone()


def update_contact_preferred_account(contact_id: int, account_id: int | None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE contacts SET preferred_email_account_id = %s WHERE id = %s",
            (account_id, contact_id),
        )


# ── Email inbox ───────────────────────────────────────────────────────────────

def get_inbox_for_task(task_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM email_inbox
            WHERE task_id = %s
            ORDER BY received_at DESC
        """, (task_id,))
        return cur.fetchall()


def get_inbox_for_contact(contact_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM email_inbox
            WHERE contact_id = %s
            ORDER BY received_at DESC
        """, (contact_id,))
        return cur.fetchall()


def save_inbox_message(provider_message_id: str, thread_id: str, contact_id, task_id,
                       from_email: str, subject: str, body: str, received_at,
                       provider: str = "gmail") -> int | None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO email_inbox
                (provider_message_id, provider, thread_id, contact_id, task_id,
                 from_email, subject, body, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider_message_id) DO NOTHING
            RETURNING id
        """, (provider_message_id, provider, thread_id, contact_id, task_id,
              from_email, subject, body, received_at))
        row = cur.fetchone()
        return row[0] if row else None


def update_inbox_summary(inbox_id: int, subject_translated: str, summary: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE email_inbox
               SET subject_translated = %s, summary = %s
             WHERE id = %s
        """, (subject_translated, summary, inbox_id))


def get_inbox_message(inbox_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM email_inbox WHERE id = %s", (inbox_id,))
        return cur.fetchone()


def get_email_history_for_contact(contact_id: int, limit: int = 20):
    """Sent + received emails for a contact, oldest first."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT 'sent'       AS direction,
                   el.sent_at   AS date,
                   el.subject,
                   el.summary   AS preview,
                   NULL         AS inbox_id
            FROM email_log el
            WHERE el.contact_id = %s

            UNION ALL

            SELECT 'received'                                   AS direction,
                   ei.received_at                              AS date,
                   COALESCE(ei.subject_translated, ei.subject) AS subject,
                   COALESCE(ei.summary, LEFT(ei.body, 200))    AS preview,
                   ei.id                                        AS inbox_id
            FROM email_inbox ei
            WHERE ei.contact_id = %s

            ORDER BY date ASC NULLS LAST
            LIMIT %s
        """, (contact_id, contact_id, limit))
        return cur.fetchall()


# ── Posts ─────────────────────────────────────────────────────────────────────

def get_all_posts():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT po.id, po.date, po.format, po.pillar, po.theme, po.image_prompt, po.image_style,
                   po.image_style_type, po.image_model_used, po.image_prompt_sent,
                   po.n_slides, po.caption, po.image_mime_type, po.video_prompt,
                   po.channels, po.linkedin_title,
                   po.project_id,
                   p.name AS project_name,
                   (po.linkedin_article_body IS NOT NULL) AS has_linkedin_article,
                   (po.image_bytes IS NOT NULL) AS has_image,
                   (po.video_bytes IS NOT NULL) AS has_video,
                   po.approved, po.posted_at, po.linkedin_posted_at, po.generated_at, po.created_at
            FROM posts po
            LEFT JOIN projects p ON p.id = po.project_id
            ORDER BY po.date
        """)
        return cur.fetchall()


def get_post(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM posts WHERE id = %s", (post_id,))
        return cur.fetchone()


def get_post_for_date(date_str: str):
    """Return all approved, unsent posts for a date including image bytes."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM posts
            WHERE date = %s AND approved = TRUE AND posted_at IS NULL
        """, (date_str,))
        return cur.fetchall()


def get_posts_for_date(date_str: str):
    """Return all posts for a date (metadata only, no binary data)."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, date, format, pillar, theme, channels, approved
            FROM posts WHERE date = %s
        """, (date_str,))
        return cur.fetchall()


def get_post_meta_by_date(date_str: str):
    """Return post metadata (no image bytes) by date string."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, date, format, pillar, theme, image_prompt, image_style,
                   image_style_type, image_model_used, n_slides, caption,
                   (image_bytes IS NOT NULL) AS has_image,
                   approved, posted_at, generated_at
            FROM posts WHERE date = %s
        """, (date_str,))
        return cur.fetchone()


def update_post_fields(post_id: int, **fields):
    """Update any combination of text fields on a post by id."""
    allowed = {"caption", "theme", "image_prompt", "image_style_type", "pillar", "n_slides",
               "image_style", "format", "video_prompt", "channels", "date",
               "linkedin_title", "linkedin_article_body", "project_id"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [post_id]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE posts SET {set_clause} WHERE id = %s", values)


def set_post_project(post_id: int, project_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET project_id = %s WHERE id = %s", (project_id, post_id))


def get_slides_for_post(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, slide_number, title_en, content, image_mime_type,
                   (image_bytes IS NOT NULL) AS has_image
            FROM slides WHERE post_id = %s ORDER BY slide_number
        """, (post_id,))
        return cur.fetchall()


def get_slides_with_images(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM slides WHERE post_id = %s ORDER BY slide_number", (post_id,))
        return cur.fetchall()


def get_slide(slide_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM slides WHERE id = %s", (slide_id,))
        return cur.fetchone()


def get_posts_pending_generation():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, date, format, pillar, theme, image_prompt, image_style,
                   image_style_type, n_slides, caption,
                   (image_bytes IS NOT NULL) AS has_image,
                   approved, posted_at, generated_at
            FROM posts
            WHERE (format != 'reel' AND (image_bytes IS NULL OR caption IS NULL))
               OR (format = 'reel' AND caption IS NULL)
            ORDER BY date
        """)
        return cur.fetchall()


def get_slides_missing_images(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM slides WHERE post_id = %s AND image_bytes IS NULL ORDER BY slide_number
        """, (post_id,))
        return cur.fetchall()


def set_post_image(post_id: int, image_bytes: bytes, mime_type: str,
                   model_used: str = "", prompt_sent: str = ""):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE posts
            SET image_bytes = %s, image_mime_type = %s,
                image_model_used = %s, image_prompt_sent = %s,
                generated_at = NOW()
            WHERE id = %s
        """, (psycopg2.Binary(image_bytes), mime_type, model_used, prompt_sent, post_id))


def set_post_caption(post_id: int, caption: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET caption = %s WHERE id = %s", (caption, post_id))


def set_slide_image(slide_id: int, image_bytes: bytes, mime_type: str = "image/jpeg"):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE slides SET image_bytes = %s, image_mime_type = %s WHERE id = %s
        """, (psycopg2.Binary(image_bytes), mime_type, slide_id))


def set_post_video_prompt(post_id: int, video_prompt: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET video_prompt = %s WHERE id = %s", (video_prompt, post_id))


def set_post_video(post_id: int, video_bytes: bytes, mime_type: str = "video/mp4"):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE posts SET video_bytes = %s, video_mime_type = %s, video_generated_at = NOW()
            WHERE id = %s
        """, (psycopg2.Binary(video_bytes), mime_type, post_id))


def clear_post_images(post_id: int):
    """Clear generated image data so it will be regenerated with the new style."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE posts
            SET image_bytes = NULL, image_mime_type = 'image/jpeg',
                image_model_used = NULL, image_prompt_sent = NULL, generated_at = NULL
            WHERE id = %s
        """, (post_id,))
        cur.execute("UPDATE slides SET image_bytes = NULL WHERE post_id = %s", (post_id,))


def set_post_approved(post_id: int, approved: bool):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET approved = %s WHERE id = %s", (approved, post_id))


def set_post_image_locked(post_id: int, locked: bool):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET image_locked = %s WHERE id = %s", (locked, post_id))


def set_post_caption_locked(post_id: int, locked: bool):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET caption_locked = %s WHERE id = %s", (locked, post_id))


def reset_post_locks(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE posts SET image_locked = FALSE, caption_locked = FALSE WHERE id = %s",
            (post_id,),
        )


def mark_post_sent(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET posted_at = NOW() WHERE id = %s", (post_id,))


def set_post_linkedin_article(post_id: int, title: str, body: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE posts SET linkedin_title = %s, linkedin_article_body = %s WHERE id = %s",
            (title, body, post_id),
        )


def mark_post_linkedin_sent(post_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE posts SET linkedin_posted_at = NOW() WHERE id = %s", (post_id,))


def ensure_slide_rows(post_id: int, n_slides: int):
    with get_conn() as conn:
        cur = conn.cursor()
        for i in range(1, n_slides + 1):
            cur.execute("""
                INSERT INTO slides (post_id, slide_number)
                VALUES (%s, %s) ON CONFLICT (post_id, slide_number) DO NOTHING
            """, (post_id, i))


def upsert_slide(post_id: int, slide: dict):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO slides (post_id, slide_number, title_en, content)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (post_id, slide_number) DO UPDATE SET
                title_en = EXCLUDED.title_en, content = EXCLUDED.content
        """, (post_id, slide["number"], slide.get("title_en"), slide.get("content")))


def create_post(date, format, pillar, theme, image_prompt=None,
                image_style=None, image_style_type="realistic",
                n_slides=None, slides=None, video_prompt=None, channels=None) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO posts (date, format, pillar, theme, image_prompt, image_style,
                               image_style_type, n_slides, video_prompt, channels)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (date, format, pillar, theme, image_prompt, image_style,
              image_style_type, n_slides, video_prompt, channels or "instagram"))
        post_id = cur.fetchone()[0]
        if slides:
            for s in slides:
                cur.execute("""
                    INSERT INTO slides (post_id, slide_number, title_en, content)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (post_id, slide_number) DO UPDATE SET
                        title_en = EXCLUDED.title_en, content = EXCLUDED.content
                """, (post_id, s["number"], s.get("title_en"), s.get("content")))
        return post_id


# ── Agents ────────────────────────────────────────────────────────────────────

def get_all_agents():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents ORDER BY is_router DESC, name")
        return cur.fetchall()


def get_router_agent():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE is_router = TRUE LIMIT 1")
        return cur.fetchone()


def get_specialist_agents():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE is_router = FALSE ORDER BY name")
        return cur.fetchall()


def get_agent_by_name(name: str):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE name = %s", (name,))
        return cur.fetchone()


def get_agent(agent_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE id = %s", (agent_id,))
        return cur.fetchone()


def upsert_agent(name: str, description: str, system_prompt: str,
                 model: str, is_router: bool = False, tool_set: str = "default") -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO agents (name, description, system_prompt, model, is_router, tool_set)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                system_prompt = EXCLUDED.system_prompt,
                model = EXCLUDED.model,
                updated_at = NOW()
            RETURNING id
        """, (name, description, system_prompt, model, is_router, tool_set))
        return cur.fetchone()[0]


def update_agent(agent_id: int, description: str, system_prompt: str, model: str,
                 name: str | None = None, tool_set: str = "default"):
    with get_conn() as conn:
        cur = conn.cursor()
        if name:
            cur.execute("""
                UPDATE agents SET name = %s, description = %s, system_prompt = %s,
                                 model = %s, tool_set = %s, updated_at = NOW()
                WHERE id = %s
            """, (name.strip().lower(), description, system_prompt, model, tool_set, agent_id))
        else:
            cur.execute("""
                UPDATE agents SET description = %s, system_prompt = %s,
                                 model = %s, tool_set = %s, updated_at = NOW()
                WHERE id = %s
            """, (description, system_prompt, model, tool_set, agent_id))


def update_agent_field_by_name(agent_name: str, field: str, new_value: str):
    """Update a single field on an agent looked up by name. Used by the router tool."""
    allowed = {"description", "system_prompt", "model"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' cannot be updated via this method")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE agents SET {field} = %s, updated_at = NOW() WHERE LOWER(name) = LOWER(%s)",
            (new_value, agent_name),
        )


def delete_agent(agent_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM agents WHERE id = %s AND is_router = FALSE", (agent_id,))


def delete_agent_by_name(agent_name: str):
    """Delete a specialist agent by name. Router agents are never deleted."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM agents WHERE LOWER(name) = LOWER(%s) AND is_router = FALSE",
            (agent_name,),
        )


# ── Brand guidelines ──────────────────────────────────────────────────────────

def get_brand_guidelines() -> str:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT content FROM brand_guidelines WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else ""


def update_brand_guidelines(content: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO brand_guidelines (id, content, updated_at)
            VALUES (1, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
        """, (content,))


# ── LinkedIn accounts ─────────────────────────────────────────────────────────

def get_all_linkedin_accounts():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM linkedin_accounts ORDER BY id")
        return cur.fetchall()


def get_connected_linkedin_accounts():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM linkedin_accounts WHERE access_token IS NOT NULL ORDER BY id"
        )
        return cur.fetchall()


def get_linkedin_account(account_id: int):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM linkedin_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()


def create_linkedin_account(label: str, client_id: str, client_secret: str,
                            callback_url: str = None) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO linkedin_accounts (label, client_id, client_secret, callback_url, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            RETURNING id
        """, (label or None, client_id or None, client_secret or None, callback_url or None))
        return cur.fetchone()[0]


def save_linkedin_config(account_id: int, label: str, client_id: str,
                         client_secret: str, callback_url: str = None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE linkedin_accounts
            SET label = %s, client_id = %s, client_secret = %s,
                callback_url = %s, updated_at = NOW()
            WHERE id = %s
        """, (label or None, client_id or None, client_secret or None,
              callback_url or None, account_id))


def save_linkedin_tokens(account_id: int, access_token: str, refresh_token: str,
                         token_expiry, linkedin_id: str, person_name: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE linkedin_accounts
            SET access_token = %s, refresh_token = %s, token_expiry = %s,
                linkedin_id = %s, person_name = %s, updated_at = NOW()
            WHERE id = %s
        """, (access_token, refresh_token, token_expiry, linkedin_id, person_name, account_id))


def update_linkedin_access_token(account_id: int, access_token: str, token_expiry):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE linkedin_accounts
            SET access_token = %s, token_expiry = %s, updated_at = NOW()
            WHERE id = %s
        """, (access_token, token_expiry, account_id))


def clear_linkedin_tokens(account_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE linkedin_accounts
            SET access_token = NULL, refresh_token = NULL, token_expiry = NULL,
                linkedin_id = NULL, person_name = NULL, updated_at = NOW()
            WHERE id = %s
        """, (account_id,))


def delete_linkedin_account(account_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM linkedin_accounts WHERE id = %s", (account_id,))


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))


# ── Email log ─────────────────────────────────────────────────────────────────

def log_email(to_name: str, to_email: str, subject: str, summary: str,
              provider_message_id: str = None, contact_id: int = None,
              task_id: int = None, email_account_id: int = None) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO email_log
                (to_name, to_email, subject, summary, provider_message_id,
                 contact_id, task_id, email_account_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (to_name, to_email, subject, summary, provider_message_id,
              contact_id, task_id, email_account_id))
        return cur.fetchone()[0]


def get_email_log(limit: int = 100):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT el.*, c.name AS contact_name
            FROM email_log el
            LEFT JOIN contacts c ON c.id = el.contact_id
            ORDER BY el.sent_at DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def get_unified_email_log(limit: int = 200):
    """Return all sent + received emails merged, newest first."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT 'sent'               AS direction,
                   el.sent_at           AS date,
                   el.to_name           AS correspondent_name,
                   el.to_email          AS correspondent_email,
                   el.subject,
                   el.summary           AS body_preview,
                   NULL                 AS body,
                   el.provider_message_id,
                   COALESCE(ea.provider, 'gmail') AS provider,
                   el.contact_id,
                   el.task_id
            FROM email_log el
            LEFT JOIN email_accounts ea ON ea.id = el.email_account_id

            UNION ALL

            SELECT 'received'                                       AS direction,
                   ei.received_at                                  AS date,
                   COALESCE(c.name, ei.from_email)                 AS correspondent_name,
                   ei.from_email                                    AS correspondent_email,
                   COALESCE(ei.subject_translated, ei.subject)     AS subject,
                   COALESCE(ei.summary, LEFT(ei.body, 300))        AS body_preview,
                   ei.body,
                   NULL                                             AS provider_message_id,
                   ei.provider,
                   ei.contact_id,
                   ei.task_id
            FROM email_inbox ei
            LEFT JOIN contacts c ON c.id = ei.contact_id

            ORDER BY date DESC NULLS LAST
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def get_unified_emails_for_task(task_id: int):
    """All sent + received emails for a task, oldest first."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT 'sent'               AS direction,
                   el.sent_at           AS date,
                   el.to_name           AS correspondent_name,
                   el.to_email          AS correspondent_email,
                   el.subject,
                   el.summary           AS body_preview,
                   NULL                 AS body,
                   el.provider_message_id,
                   COALESCE(ea.provider, 'gmail') AS provider
            FROM email_log el
            LEFT JOIN email_accounts ea ON ea.id = el.email_account_id
            WHERE el.task_id = %s

            UNION ALL

            SELECT 'received'                                       AS direction,
                   ei.received_at                                  AS date,
                   COALESCE(c.name, ei.from_email)                 AS correspondent_name,
                   ei.from_email                                    AS correspondent_email,
                   COALESCE(ei.subject_translated, ei.subject)     AS subject,
                   COALESCE(ei.summary, LEFT(ei.body, 300))        AS body_preview,
                   ei.body,
                   NULL                                             AS provider_message_id,
                   ei.provider
            FROM email_inbox ei
            LEFT JOIN contacts c ON c.id = ei.contact_id
            WHERE ei.task_id = %s

            ORDER BY date ASC NULLS LAST
        """, (task_id, task_id))
        return cur.fetchall()


def get_unified_emails_for_contact(contact_id: int):
    """All sent + received emails for a contact, newest first."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT 'sent'               AS direction,
                   el.sent_at           AS date,
                   el.to_name           AS correspondent_name,
                   el.to_email          AS correspondent_email,
                   el.subject,
                   el.summary           AS body_preview,
                   NULL                 AS body,
                   el.provider_message_id,
                   COALESCE(ea.provider, 'gmail') AS provider,
                   el.task_id
            FROM email_log el
            LEFT JOIN email_accounts ea ON ea.id = el.email_account_id
            WHERE el.contact_id = %s

            UNION ALL

            SELECT 'received'                                       AS direction,
                   ei.received_at                                  AS date,
                   COALESCE(c.name, ei.from_email)                 AS correspondent_name,
                   ei.from_email                                    AS correspondent_email,
                   COALESCE(ei.subject_translated, ei.subject)     AS subject,
                   COALESCE(ei.summary, LEFT(ei.body, 300))        AS body_preview,
                   ei.body,
                   NULL                                             AS provider_message_id,
                   ei.provider,
                   ei.task_id
            FROM email_inbox ei
            LEFT JOIN contacts c ON c.id = ei.contact_id
            WHERE ei.contact_id = %s

            ORDER BY date DESC NULLS LAST
        """, (contact_id, contact_id))
        return cur.fetchall()
