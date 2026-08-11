import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

_client = None

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "needs_work"]},
        "overall_score": {"type": "integer"},
        "summary": {"type": "string"},
        "tone": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "notes": {"type": "string"}},
            "required": ["score", "notes"],
        },
        "human_voice": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "notes": {"type": "string"}},
            "required": ["score", "notes"],
        },
        "clarity": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "notes": {"type": "string"}},
            "required": ["score", "notes"],
        },
        "cta_strength": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "notes": {"type": "string"}},
            "required": ["score", "notes"],
        },
        "grammar": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "notes": {"type": "string"}},
            "required": ["score", "notes"],
        },
        "seo_basics": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "notes": {"type": "string"}},
            "required": ["score", "notes"],
        },
    },
    "required": [
        "verdict",
        "overall_score",
        "summary",
        "tone",
        "human_voice",
        "clarity",
        "cta_strength",
        "grammar",
        "seo_basics",
    ],
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


def build_prompt(draft, channel, brief=None):
    brand_voice = _load_brand_voice()

    # Several brand rules are conditional on what the piece was asked to be —
    # section 3's one-person rule is waived for pieces that explicitly call for
    # market-level framing, and section 8's CTA expectation doesn't apply to a
    # piece never meant to carry one. Without the brief the Reviewer can only
    # guess at intent from the prose, so it defaults to penalizing. When the
    # caller knows the brief, pass it.
    brief_block = ""
    if brief:
        asked_for = "\n".join(f"- {k}: {v}" for k, v in brief.items() if v)
        if asked_for:
            brief_block = f"""
WHAT THIS PIECE WAS ASKED TO BE:
{asked_for}

Judge the draft against this brief, not against a generic ideal. Where a brand rule
has a stated exception and the brief invokes it, apply the exception rather than the
default rule — and say so in the notes. Do not penalize the draft for lacking
something the brief never asked for.
"""

    return f"""You are the Reviewer Agent for CreditChek's marketing team.
Review the draft below against the brand voice doc and general content quality standards for the given channel.

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---

TARGET CHANNEL: {channel}
{brief_block}
DRAFT TO REVIEW:
---
{draft}
---

Score each category 0-100 and give concise, actionable notes (1-2 sentences each):
- tone: does it match the brand voice doc's tone and vocabulary rules? Check section 3 of the doc ("one person, one problem, one solution") explicitly and score against it as written, including its stated exception. Penalize violations of it specifically and name the rule in the notes.
- human_voice: does this read like a person wrote it, or like a model did? Score down for: an even,
  steady sentence rhythm with no variation in length; scene-setting openers ("In today's fast-paced
  world...", "In an era of..."); formulaic transitions ("Moreover," "Furthermore," "In conclusion");
  reflexive rule-of-three lists; hedging filler ("it's important to note," "can potentially"); a tidy
  closing restatement of what was just said; overused model-vocabulary ("delve," "unpack," "leverage,"
  "seamless," "robust," "landscape," "ecosystem," "elevate," "underscore"); or em-dashes used as a
  default way to bolt on a clause. Name the specific tell(s) found, in the notes.
- clarity: is it easy to understand, one idea per piece, no unexplained jargon?
- cta_strength: is the CTA (if any) clear and singular? If there's no CTA, note that.
- grammar: spelling, grammar, punctuation issues.
- seo_basics: only meaningful for blog/email/website content. For social posts, score scannability/hook strength instead and note that SEO doesn't apply to this channel.

overall_score is a 0-100 weighted sense of the whole draft.
verdict is "pass" if overall_score >= 75 and no category flags a serious problem, otherwise "needs_work".
summary is 2-3 sentences on the single most important thing to fix, or confirmation it's ready as-is.
"""


def review_draft(draft, channel, brief=None):
    """Score a draft. `brief` is optional — pass it when the caller knows what
    the piece was asked to be, so conditional brand rules resolve correctly."""
    if not draft or not channel:
        raise ValueError("draft and channel are required")

    client = _get_client()
    prompt = build_prompt(draft, channel, brief)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=REVIEW_SCHEMA,
        ),
    )
    return json.loads(response.text)
