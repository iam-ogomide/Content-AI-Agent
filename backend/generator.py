import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai

from product_context import load_product_context

load_dotenv()

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client = None

# Hard platform limits and house length conventions per channel. The platform
# limits (X's 280 chars, Instagram's 2200) are enforced by the channel itself;
# the rest are conventions that keep drafts publishable without editing down.
CHANNEL_LIMITS = {
    "X (Twitter)": "280 characters maximum — this is a hard platform limit. Count characters, not words. If the idea will not fit, cut it down rather than running over.",
    "LinkedIn": "Aim for 150-250 words. The first 2 lines must hook before the 'see more' cutoff.",
    "Instagram": "Aim for 50-125 words. 2200 characters is the hard platform cap.",
    "Email": "Aim for 150-200 words in the body. Subject line under 50 characters.",
    "Blog": "600-1200 words unless the brief says otherwise.",
    "Graphic": "Headline under 10 words. Supporting text 1-2 short sentences. One CTA. No paragraphs.",
}

# Channels that hand off structured ad/design copy rather than flowing prose.
# The Generator's usual "no markdown, plain paragraph" instructions don't
# apply here — a designer needs the three parts clearly separated.
STRUCTURED_CHANNELS = {"Graphic"}


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


def build_prompt(topic, channel, tone, audience=None, cta=None, keyword=None, word_limit=None,
                 previous_draft=None, revision_note=None):
    brand_voice = _load_brand_voice()

    # brand_voice.md gives tone and a short product summary; product.pdf is the
    # internal handover doc with the depth behind it (what's actually live,
    # shipping, or still planned, per-product specifics brand_voice.md never
    # gets into). Optional: an empty string here just means the doc wasn't
    # found, and the prompt below drops the block instead of failing.
    product_context = load_product_context()
    product_block = ""
    if product_context:
        product_block = f"""
--- PRODUCT CONTEXT (internal handover doc) ---
{product_context}
--- END PRODUCT CONTEXT ---

This doc is for factual grounding only — get product names, capabilities, and status (live /
shipping / planned / in development) right, and never present a planned or in-development
item as available now. It contains internal details (contacts, emails, roadmap risk notes)
that must never appear in the output, and must never be quoted from directly or mentioned as
a source. If it conflicts with the brand voice doc on a product's current status, this doc is
more current — the brand voice doc still governs tone and what to claim publicly.
"""

    brief_lines = [
        f"Topic: {topic}",
        f"Channel: {channel}",
        f"Tone: {tone}",
    ]
    if audience:
        brief_lines.append(f"Audience: {audience}")
    if cta:
        brief_lines.append(f"CTA: {cta}")
    if keyword:
        brief_lines.append(f"Keyword to include: {keyword}")
    if word_limit:
        brief_lines.append(f"Hard word limit: {word_limit} words maximum")
    brief = "\n".join(brief_lines)

    length_rule = CHANNEL_LIMITS.get(channel, "Respect the target channel's typical length and format.")
    if word_limit:
        length_rule = f"Hard limit: {word_limit} words maximum. This overrides the channel default. ({length_rule})"

    if channel in STRUCTURED_CHANNELS:
        output_rule = (
            "Output exactly three lines, plain text, no markdown:\n"
            "Headline: <the headline>\n"
            "Supporting text: <one to two short sentences>\n"
            "CTA: <the call to action>\n"
            "This is copy for a designer to lay out, not a finished post — no extra lines, "
            "no design notes, no explanation of the concept."
        )
    else:
        output_rule = (
            "Output plain text only. No markdown of any kind — no headers, no **bold**, no "
            "backticks, no bullet syntax. If the brief gives a keyword, weave it into a "
            "sentence as ordinary words."
        )

    # Revising an existing draft is a different job from writing one, so the task
    # line and an extra block change when a previous draft is supplied. Nothing
    # here applies to a first draft.
    revision_block = ""
    task_line = "Write a first-draft piece of content strictly following the brand voice doc below."
    if previous_draft and revision_note:
        task_line = "Revise an existing draft, strictly following the brand voice doc below."
        revision_block = f"""
--- PREVIOUS DRAFT ---
{previous_draft}
--- END PREVIOUS DRAFT ---

REVISION REQUESTED: {revision_note}

Revise the draft above. Keep everything that already works — wording, structure, and
phrasing the request does not touch. Change only what the request asks for. Do not
rewrite from scratch, and do not add new claims or sections that were not asked for.
"""

    return f"""You are the Generator Agent for CreditChek's marketing team.
{task_line}

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---
{product_block}
CONTENT BRIEF:
{brief}
{revision_block}
LENGTH REQUIREMENT (non-negotiable):
{length_rule}

Instructions:
- Write only the content itself. No preamble, no explanation, no sign-off to the reader about what you did.
- {output_rule}
- Match the tone and channel conventions described in the brand voice doc.
- If a CTA is given, end with it clearly.
- The length requirement above is a constraint, not a target to fill. Shorter is fine.
- Section 3 of the doc ("one person, one problem, one solution") is non-negotiable. Follow it as written, including its exception for pieces that explicitly call for market-level framing.
"""


def generate_draft(topic, channel, tone, audience=None, cta=None, keyword=None, word_limit=None,
                   previous_draft=None, revision_note=None):
    if not topic or not channel or not tone:
        raise ValueError("topic, channel, and tone are required")

    client = _get_client()
    prompt = build_prompt(topic, channel, tone, audience, cta, keyword, word_limit,
                          previous_draft, revision_note)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    return response.text


def generate_draft_stream(topic, channel, tone, audience=None, cta=None, keyword=None, word_limit=None,
                          previous_draft=None, revision_note=None):
    """Same call as generate_draft, but yields text as Gemini streams it back."""
    if not topic or not channel or not tone:
        raise ValueError("topic, channel, and tone are required")

    client = _get_client()
    prompt = build_prompt(topic, channel, tone, audience, cta, keyword, word_limit,
                          previous_draft, revision_note)

    for chunk in client.models.generate_content_stream(model=MODEL_NAME, contents=prompt):
        if chunk.text:
            yield chunk.text
