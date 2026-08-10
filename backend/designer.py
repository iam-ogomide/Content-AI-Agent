"""Designer Agent — turns a piece of content into a matching visual via Canva.

WHAT THIS PLAN CAN AND CAN'T DO (Canva Business):
  - Design generation from a text prompt, editing, resizing, export: available.
  - Autofilling a fixed CreditChek brand template with just the copy (the thing
    that would guarantee every visual looks identical) is Enterprise-only and
    is NOT available on Business. This agent therefore prompts Canva to build
    a fresh design each call and leans on the brand voice doc + CHANNEL_SPECS
    below to keep results as consistent as prompting can make them.
  - If the account is ever upgraded to Enterprise, replace generate_visual's
    prompt-a-fresh-design approach with a call to Canva's autofill tool
    against one saved brand template ID. That is the version worth wanting.

AUTH — READ BEFORE USING:
  Canva's MCP server authenticates per Canva user via OAuth, not a
  service-account API key like GEMINI_API_KEY — see canva_auth.py for the
  implementation and its one-time SETUP steps. It caches tokens to a local
  file, which is fine for now (nothing is deployed yet) but MUST change to a
  real secrets store before this runs anywhere without a persistent local
  disk — that's flagged in canva_auth.py's own docstring too.

Requires the `mcp` package: pip install mcp
"""

import asyncio
import os
from pathlib import Path

import httpx2
from canva_auth import get_canva_auth
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CANVA_MCP_URL = os.getenv("CANVA_MCP_URL", "https://mcp.canva.com/mcp")

_client = None

# Target dimensions per channel. Kept here rather than in generator.py because
# these describe the graphic, not the copy — a LinkedIn post and its graphic
# have different size constraints entirely.
CHANNEL_SPECS = {
    "Instagram": "1080x1080 square post",
    "LinkedIn": "1200x627 landscape post",
    "X (Twitter)": "1600x900 landscape post",
    "Email": "600px-wide header graphic",
    "Blog": "1200x630 landscape header image",
}


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or api_key == "your-gemini-api-key-here":
            raise RuntimeError("GEMINI_API_KEY is not set. Add your real key to .env")
        _client = genai.Client(api_key=api_key)
    return _client


def _load_brand_voice():
    return BRAND_VOICE_PATH.read_text(encoding="utf-8")


def build_prompt(topic, channel, headline=None, draft_excerpt=None, style_note=None):
    brand_voice = _load_brand_voice()
    dims = CHANNEL_SPECS.get(channel, "a square social graphic")

    brief_lines = [f"Topic: {topic}", f"Channel: {channel}", f"Format: {dims}"]
    if headline:
        brief_lines.append(f"Headline text to feature on the design: {headline}")
    if draft_excerpt:
        # Context only — the graphic should never carry the whole post as text.
        brief_lines.append(
            "Related post copy, for context only, do not put all of this on "
            f"the design: {draft_excerpt[:400]}"
        )
    if style_note:
        brief_lines.append(f"Style note: {style_note}")
    brief = "\n".join(brief_lines)

    return f"""You are the Designer Agent for CreditChek's marketing team.
Use the Canva tools available to you to create ONE on-brand visual for the brief below,
then export it and report the export URL.

--- BRAND VOICE DOC (use for tone, color, and typography cues where it gives them) ---
{brand_voice}
--- END BRAND VOICE DOC ---

DESIGN BRIEF:
{brief}

Instructions:
- Create a single design sized for the format given above.
- Keep on-design text minimal: a headline and, at most, one short supporting line. This
  is a graphic to accompany a post, not the post itself — do not try to fit the full copy
  on it.
- Follow the brand voice doc's tone and any stated visual cues (colors, typography).
  Do not invent brand colors or fonts the doc does not mention — if it is silent on
  visual style, keep the design clean and minimal rather than guessing at a look.
- Export the finished design and state its export URL clearly in your final response.
"""


async def _generate_visual_async(topic, channel, headline, draft_excerpt, style_note):
    prompt = build_prompt(topic, channel, headline, draft_excerpt, style_note)
    client = _get_client()

    # NOTE: this version of the mcp package (2.0.0) vendors its own httpx fork
    # (httpx2) and no longer takes auth= directly on streamable_http_client —
    # the OAuth provider has to be attached to an httpx2.AsyncClient instead.
    # If you upgrade mcp later and this breaks again, check
    # inspect.signature(streamable_http_client) first rather than guessing.
    http_client = httpx2.AsyncClient(auth=get_canva_auth())

    async with streamable_http_client(CANVA_MCP_URL, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=genai_types.GenerateContentConfig(tools=[session]),
            )
    return response.text


def generate_visual(topic, channel, headline=None, draft_excerpt=None, style_note=None):
    """Sync wrapper so this agent's call shape matches generate_draft / repurpose_content.

    Returns the model's final text turn, which — per the prompt above — should
    contain the Canva export URL. This is not currently parsed out into a
    clean field; do that once you see what real responses look like.
    """
    if not topic or not channel:
        raise ValueError("topic and channel are required")

    return asyncio.run(
        _generate_visual_async(topic, channel, headline, draft_excerpt, style_note)
    )


if __name__ == "__main__":
    # Manual first-run test — this is what triggers the one-time Canva browser
    # login (see canva_auth.py's SETUP notes). Run with:  python designer.py
    result = generate_visual(
        topic="CreditChek's BVN liveness check",
        channel="LinkedIn",
        headline="Verify in seconds, not days",
    )
    print("\n--- RESULT ---")
    print(result)