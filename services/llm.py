"""Anthropic LLM utilities — single client, retry wrapper, system prompt builder."""

import asyncio
import logging
import os
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
_RETRY_DELAYS = [10, 30, 60]
_RATE_LIMIT_DELAYS = [60, 120, 180]

anthropic_client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


async def call_claude(**kwargs):
    """Call Anthropic API with automatic retry on 529 overload and 429 rate limit."""
    delays = list(zip(_RETRY_DELAYS, _RATE_LIMIT_DELAYS))
    for attempt, (overload_delay, rate_delay) in enumerate(delays, start=1):
        try:
            return await anthropic_client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                logger.warning("Anthropic overloaded (attempt %d), retrying in %ds", attempt, overload_delay)
                await asyncio.sleep(overload_delay)
            elif e.status_code == 429:
                logger.warning("Anthropic rate limit hit (attempt %d), retrying in %ds", attempt, rate_delay)
                await asyncio.sleep(rate_delay)
            else:
                raise
    return await anthropic_client.messages.create(**kwargs)


def build_brand_prompt(extra: str | None = None) -> str:
    """Build a system prompt with only brand context (no agent instructions).

    Use for content generation that needs brand voice but doesn't belong to a specific agent.
    """
    import db

    brand = db.get_brand_guidelines()
    parts = [f"BRAND CONTEXT:\n{brand}"]
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def build_system_prompt(agent_name: str, extra: str | None = None) -> str:
    """Build a system prompt from brand guidelines + agent instructions + optional extra block.

    For router agents, injects an AVAILABLE AGENTS block listing all specialist agents.
    """
    import db

    brand = db.get_brand_guidelines()

    agent = db.get_agent_by_name(agent_name)
    if agent:
        agent_prompt = agent["system_prompt"]
        is_router = agent.get("is_router", False)
    else:
        agent_prompt = f"Agent '{agent_name}' not found."
        is_router = False

    parts = [f"BRAND CONTEXT:\n{brand}", f"AGENT INSTRUCTIONS:\n{agent_prompt}"]

    if is_router:
        specialists = db.get_specialist_agents()
        if specialists:
            lines = ["AVAILABLE AGENTS:"]
            for s in specialists:
                lines.append(f"- {s['name']} ({s['model']}): {s['description']}")
            parts.append("\n".join(lines))

    if extra:
        parts.append(extra)
    return "\n\n".join(parts)
