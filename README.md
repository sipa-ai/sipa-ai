# SIPA — Simple Personal Assistant AI

A self-hosted, multi-agent AI assistant framework built on Claude. One Telegram bot, one web portal, any number of specialist agents — all configurable at runtime without touching code.

---

## What it does

SIPA connects to your Telegram and gives you a personal AI team. A **router agent** handles all incoming messages and delegates to **specialist agents** you create. Out of the box it manages contacts, email, a content calendar, and LinkedIn — but you can extend it however you need by creating new agents via Telegram or the web portal.

---

## Key features

- **Agentic router** — all Telegram messages go through one entry point; the router decides whether to handle or delegate
- **Specialist agents** — create, update, and delete agents at runtime via Telegram or the portal (`/team`)
- **Contact management** — address book with email history and task pipeline
- **Email integration** — Gmail and Outlook OAuth; agent drafts, you approve, it sends; inbox polling with AI summaries
- **Content calendar** — plan Instagram and LinkedIn posts, generate captions and images with Gemini, approve in the portal
- **Scheduled reports** — daily and weekly summaries driven by sections in the router's system prompt
- **Web portal** — FastAPI admin UI at `/` for managing posts, tasks, contacts, agents, and settings
- **Brand context** — one `context/brand.md` file injected into every agent's system prompt
- **Multi-provider email** — Gmail and Outlook in parallel, routes by contact preference

---

## Architecture

```
Telegram message
       │
  Router agent  ──── tool call ────▶  Specialist agent
       │                                    │
  Tool execution ◀───────────────────────────
       │
  PostgreSQL DB   FastAPI portal   APScheduler jobs
```

There is always exactly one router agent (`is_router = TRUE` in the `agents` table). Specialist agents live only in the database — no files to manage. The router's system prompt is extended at runtime with an `AVAILABLE AGENTS` block, so adding an agent instantly changes what the router can delegate to.

See [CLAUDE.md](CLAUDE.md) for a full architecture reference.

---

## Prerequisites

- Python 3.11+
- PostgreSQL database
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Anthropic API key
- Google AI Studio key (optional — for Gemini image generation)

---

## Quick start

```bash
git clone https://github.com/sipa-ai/sipa-ai.git
cd sipa-ai
pip install -r requirements.txt
```

Copy the environment template and fill in your values:

```bash
cp app.yaml.example app.yaml
# edit app.yaml with your tokens, database URL, and credentials
```

Edit your brand context:

```
context/brand.md
```

This file is injected into every agent's system prompt. Fill in your organisation name, audience, voice, and any guidelines you want agents to follow.

Run locally:

```bash
DATABASE_URL=postgresql://... TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... python main.py
```

On first boot the database schema is created and the router agent is seeded automatically.

---

## Deploy to DigitalOcean App Platform

1. Fork this repo and push to your GitHub account
2. Create a PostgreSQL database in DigitalOcean
3. Copy `app.yaml.example` to `app.yaml`, fill in all values
4. In the DigitalOcean dashboard: **Create App → From GitHub → select your repo**

The `app.yaml` is gitignored — never commit your real secrets.

---

## Configuration

All runtime settings are stored in the database and editable via the portal at `/settings`:

| Setting | Description |
|---|---|
| `owner_name` | Your name — injected into agent tool descriptions for approval prompts |
| `agent_language` | Language agents use for summaries and reports |
| `allowed_user_ids` | Comma-separated Telegram user IDs allowed to message the bot |
| `google_ai_studio_key` | API key for Gemini image generation |
| `gemini_image_model` | Comma-separated list of Gemini models to try in order |

Email accounts and LinkedIn accounts are managed at `/settings/email` and `/settings/linkedin`.

---

## Creating specialist agents

Via Telegram:
> *"Create a new team member who handles social media content"*

The router generates a system prompt, shows it to you for confirmation, then saves the agent. It is immediately available for delegation.

Via the portal: go to `/team/new`.

---

## Giving agents extra tools

By default, specialist agents get read-only tools (`get_all_tasks`, `get_all_posts`). To give an agent write access, map its name in `_AGENT_TOOLS` in `bot.py`:

```python
_AGENT_TOOLS = {
    "content": _CONTENT_TOOLS,  # create_post, update_post, + read tools
}
```

---

## LinkedIn setup

1. Go to [developers.linkedin.com](https://www.linkedin.com/developers/) and create an app
2. Under the **Products** tab, request access to:
   - **Sign In with LinkedIn using OpenID Connect** (unlocks `openid`, `profile`, `email`) — approved instantly
   - **Share on LinkedIn** (unlocks `w_member_social`) — may require a short review
3. Once both products show as *Added*, go to the **Auth** tab and confirm these scopes are listed: `openid`, `profile`, `email`, `w_member_social`
4. Add your callback URL to the **Authorized Redirect URLs**: `https://your-domain/settings/linkedin/callback`
5. In the portal go to `/settings/linkedin`, create an account with your Client ID and Client Secret, and click **Connect**

> If you get `unauthorized_scope_error` during OAuth, the Products have not been approved yet or the redirect URL doesn't exactly match.

---

## Scheduled reports

Add a `## Daily Report` or `## Weekly Report` section to the router's system prompt (editable in the portal at `/team`). The section body tells the router what to do — it runs as a full agentic loop and sends the result to Telegram.

If neither section exists, no report is sent.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
