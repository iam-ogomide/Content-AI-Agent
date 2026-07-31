import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

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


def build_prompt(draft, channel):
    brand_voice = _load_brand_voice()

    return f"""You are the Reviewer Agent for CreditChek's marketing team.
Review the draft below against the brand voice doc and general content quality standards for the given channel.

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---

TARGET CHANNEL: {channel}

DRAFT TO REVIEW:
---
{draft}
---

Score each category 0-100 and give concise, actionable notes (1-2 sentences each):
- tone: does it match the brand voice doc's tone and vocabulary rules? Also check the one-person rule explicitly: does it address a single reader as "you" from the first sentence, with one problem and one solution — rather than opening with a crowd/third-person framing like "many businesses" or "lenders across Africa"? Penalize violations of this rule specifically and name it in the notes.
- clarity: is it easy to understand, one idea per piece, no unexplained jargon?
- cta_strength: is the CTA (if any) clear and singular? If there's no CTA, note that.
- grammar: spelling, grammar, punctuation issues.
- seo_basics: only meaningful for blog/email/website content. For social posts, score scannability/hook strength instead and note that SEO doesn't apply to this channel.

overall_score is a 0-100 weighted sense of the whole draft.
verdict is "pass" if overall_score >= 75 and no category flags a serious problem, otherwise "needs_work".
summary is 2-3 sentences on the single most important thing to fix, or confirmation it's ready as-is.
"""


def review_draft(draft, channel):
    if not draft or not channel:
        raise ValueError("draft and channel are required")

    client = _get_client()
    prompt = build_prompt(draft, channel)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=REVIEW_SCHEMA,
        ),
    )
    return json.loads(response.text)
