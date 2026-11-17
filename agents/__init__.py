"""Eleven YouTube growth agents.

Each agent is a focused prompt over the same studio context: streamer, platform,
clip transcript, recent titles and any extra notes. Output is saved both to
state (for the dashboard) and as a Markdown file in agent_outputs/.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import ai
from config import DIRS
from state import get_state
from utils import LOG, now_iso, safe_name

SYSTEM = """You are an elite short-form content operator for gaming and streaming channels.
You are direct, specific and practical. Never give generic advice. Use concrete
numbers, titles, hooks and next actions. Write for a creator who is smart but busy.
Use short markdown sections with clear headings. No filler, no disclaimers."""

AGENTS: list[dict[str, Any]] = [
    {
        "id": "viral-hook-doctor",
        "name": "Viral Hook Doctor",
        "emoji": "🪝",
        "group": "Clips",
        "description": "Rewrites the first 3 seconds so viewers stop scrolling.",
        "prompt": """Diagnose the opening of this clip and fix it.

STREAMER: {streamer} ({platform})
CLIP TITLE: {title}
TRANSCRIPT:
{transcript}

Give:
1. Why the first 3 seconds win or lose (specific, quote the current opening).
2. Five replacement hooks, max 8 words each, written as on-screen text.
3. The single best hook and the exact frame/second to show it.
4. A 2-line caption that matches the best hook.
5. One pattern-interrupt edit (zoom, cut, sound) with a timestamp.""",
    },
    {
        "id": "title-lab",
        "name": "Title Lab",
        "emoji": "🏷️",
        "group": "Clips",
        "description": "Ten titles across curiosity, search and humor.",
        "prompt": """Write titles for this clip.

STREAMER: {streamer}
PLATFORM: {platform}
TRANSCRIPT:
{transcript}

Return a markdown table with 10 rows: Title | Type (curiosity/search/humor/rage) | Why it works.
Rules: max 55 characters, no hashtags, no quotes, never generic like "insane moment".
Then pick the top 3 and say which platform each one fits best.""",
    },
    {
        "id": "thumbnail-psychologist",
        "name": "Thumbnail Psychologist",
        "emoji": "🖼️",
        "group": "Clips",
        "description": "Shot list, expression, text and color for a high-CTR frame.",
        "prompt": """Design the thumbnail frame for this clip.

STREAMER: {streamer}
TITLE: {title}
WHAT HAPPENED: {transcript}

Give:
1. Exact frame to grab (second + why).
2. Face/expression direction (or "no face" if the moment is better without).
3. Three thumbnail text options, max 4 words each.
4. Color/contrast notes that survive a 120px mobile preview.
5. A/B test plan: two variants, what each one tests.""",
    },
    {
        "id": "shorts-algorithm-analyst",
        "name": "Shorts Algorithm Analyst",
        "emoji": "📈",
        "group": "Strategy",
        "description": "Explains why a clip would or would not get pushed.",
        "prompt": """Act as a YouTube Shorts algorithm analyst.

CLIP: {title}
STREAMER: {platform} — {streamer}
TRANSCRIPT:
{transcript}

Score the clip 0-100 for distribution and explain the score with the signals the
algorithm actually measures (swipe-away in first 2s, watch-through, rewatches,
shares, comments). Then give:
1. Three changes that would raise the score by 10+ points.
2. Best posting window and why (assume US gaming audience).
3. Whether this should be a Short, a TikTok, or a long-form cut — and why.""",
    },
    {
        "id": "retention-surgeon",
        "name": "Retention Surgeon",
        "emoji": "🩺",
        "group": "Strategy",
        "description": "Finds the exact seconds viewers will drop off and fixes them.",
        "prompt": """Perform retention surgery on this clip.

CLIP: {title}
TRANSCRIPT WITH TIMING CONTEXT:
{transcript}

Produce:
1. Predicted retention curve described in 5-second buckets.
2. The three most likely drop-off moments and the cause of each.
3. A cut list: what to remove, what to speed up 1.25x, where to add a zoom or sound.
4. A rewritten opening line that buys 3 extra seconds.
5. Target runtime for this specific moment.""",
    },
    {
        "id": "channel-auditor",
        "name": "Channel Auditor",
        "emoji": "🔍",
        "group": "Channel",
        "description": "Audits positioning, consistency and the next 5 uploads.",
        "prompt": """Audit this creator's channel positioning.

CREATOR: {streamer} on {platform}
RECENT CLIP TITLES / CONTEXT:
{transcript}
EXTRA NOTES: {extra}

Deliver:
1. What the channel is currently known for, in one sentence a fan would say.
2. Three positioning options with a realistic ceiling for each.
3. The next 5 uploads, in order, with title + hook + why it fits the arc.
4. What to stop posting immediately.
5. A one-line channel bio and a pinned-comment strategy.""",
    },
    {
        "id": "posting-calendar",
        "name": "Posting Calendar",
        "emoji": "🗓️",
        "group": "Channel",
        "description": "A 14-day multi-platform schedule with clip reuse.",
        "prompt": """Build a 14-day posting calendar for this creator.

CREATOR: {streamer} ({platform})
START DATE: {date}
AVAILABLE CLIP CONTEXT:
{transcript}
EXTRA: {extra}

Return a markdown table: Day | Platform | Format | Title/Hook | Source clip | Goal.
Rules: 1-2 posts per day maximum, no duplicate hooks within 48h, reuse one clip
across platforms with different openings, include two "experiment" slots.
End with the three metrics to review on day 15 and the decision rule for each.""",
    },
    {
        "id": "monetization-strategist",
        "name": "Monetization Strategist",
        "emoji": "💰",
        "group": "Money",
        "description": "Maps clip output to Whop campaigns, RPM and offers.",
        "prompt": """Turn this creator's clip output into a monetization plan.

CREATOR: {streamer} ({platform})
CLIP: {title}
CONTEXT:
{transcript}
EXTRA: {extra}

Deliver:
1. Three realistic revenue paths ranked by speed to first $100.
2. A Whop campaign structure: offer, price, payout per 1k views, what the clipper gets.
3. The clip types that convert best for this audience and why.
4. A simple tracker: the exact columns to log weekly.
5. A 30-day target with the math (views needed at the assumed RPM).""",
    },
    {
        "id": "comment-miner",
        "name": "Comment Miner",
        "emoji": "💬",
        "group": "Audience",
        "description": "Turns reactions into the next 10 clip ideas.",
        "prompt": """Mine the audience reaction for content.

CLIP: {title}
TRANSCRIPT / CHAT CONTEXT:
{transcript}
EXTRA COMMENTS: {extra}

Deliver:
1. The five emotions the audience is actually expressing, with example phrasings.
2. Ten follow-up clip ideas ranked by likely performance, each with a hook.
3. Three reply templates the creator can use on the top comments.
4. One community post or poll that would boost the next upload.""",
    },
    {
        "id": "competitor-spy",
        "name": "Competitor Spy",
        "emoji": "🕵️",
        "group": "Strategy",
        "description": "Reverse-engineers what is working for similar channels.",
        "prompt": """Analyze the competitive landscape for this creator.

CREATOR: {streamer} ({platform})
NICHE CONTEXT:
{transcript}
EXTRA: {extra}

Deliver:
1. Three competitor archetypes (not names) and the format each owns.
2. What they do in the first 2 seconds that this creator does not.
3. Five formats this creator can steal and adapt this week.
4. A gap nobody in this niche is filling yet.
5. One experiment with a success metric and a kill criterion.""",
    },
    {
        "id": "repurpose-engine",
        "name": "Repurpose Engine",
        "emoji": "♻️",
        "group": "Clips",
        "description": "Splits one clip into a week of posts across platforms.",
        "prompt": """Repurpose this single clip into a week of content.

CLIP: {title}
STREAMER: {streamer} ({platform})
TRANSCRIPT:
{transcript}

Return:
1. Seven posts (one per day), each with platform, format, hook and the exact
   seconds of this clip to use.
2. Three text posts (X/Twitter, community, Discord) derived from the same moment.
3. Two quote-card or meme ideas with the exact line to put on screen.
4. A description of how to chain them so each post teases the next.""",
    },
]

AGENTS_BY_ID = {a["id"]: a for a in AGENTS}


def list_agents() -> list[dict[str, Any]]:
    return [
        {k: a[k] for k in ("id", "name", "emoji", "group", "description")}
        for a in AGENTS
    ]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    return AGENTS_BY_ID.get(agent_id)


def build_prompt(agent: dict[str, Any], context: dict[str, Any]) -> str:
    values = {
        "streamer": str(context.get("streamer") or "the streamer")[:80],
        "platform": str(context.get("platform") or "twitch")[:24],
        "title": str(context.get("title") or "untitled clip")[:120],
        "transcript": str(context.get("transcript") or "(no transcript captured)")[-6000:],
        "extra": str(context.get("extra") or "(none)")[:2000],
        "date": str(context.get("date") or datetime.now().strftime("%Y-%m-%d")),
    }
    for key, value in values.items():
        if "{" + key + "}" in agent["prompt"]:
            continue
        values[key] = value
    return agent["prompt"].format(**values)


def run_agent(agent_id: str, context: dict[str, Any] | None = None,
              *, max_tokens: int = 2600) -> dict[str, Any]:
    state = get_state()
    agent = get_agent(agent_id)
    if not agent:
        raise KeyError(f"Unknown agent: {agent_id}")
    if not ai.enabled():
        raise ai.AIError(
            "Agents need an AI endpoint. Set MY_AI_ENDPOINT_URL and MY_AI_API_KEY in .env."
        )
    prompt = build_prompt(agent, context or {})
    started = time.time()
    output = ai.agent_run(SYSTEM, prompt, max_tokens=max_tokens, temperature=0.65)
    elapsed = round(time.time() - started, 1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{safe_name(agent['name'], 40)}.md"
    path = DIRS["agent_outputs"] / name
    body = (
        f"# {agent['name']}\n\n"
        f"- Streamer: {str((context or {}).get('streamer') or '—')}\n"
        f"- Platform: {str((context or {}).get('platform') or '—')}\n"
        f"- Clip: {str((context or {}).get('title') or '—')}\n"
        f"- Generated: {now_iso()} ({elapsed}s)\n\n"
        f"---\n\n{output.strip()}\n"
    )
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not write agent output: %s", exc)

    record = {
        "id": f"{agent_id}-{int(time.time())}",
        "agent_id": agent_id,
        "agent": agent["name"],
        "emoji": agent["emoji"],
        "created_at": now_iso(),
        "elapsed": elapsed,
        "streamer": str((context or {}).get("streamer") or ""),
        "title": str((context or {}).get("title") or ""),
        "output": output.strip()[:20000],
        "file": name,
    }
    state.agent_outputs.append(record)
    state.save_agent_outputs()
    state.add_event("success", f"{agent['name']} finished in {elapsed}s")
    return record


def delete_output(output_id: str) -> bool:
    state = get_state()
    before = len(state.agent_outputs)
    state.agent_outputs = [o for o in state.agent_outputs if o.get("id") != output_id]
    if len(state.agent_outputs) == before:
        return False
    state.save_agent_outputs()
    return True


def read_output(filename: str) -> str:
    path = DIRS["agent_outputs"] / safe_name(Path(filename).name, 120)
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path.read_text(encoding="utf-8", errors="replace")
