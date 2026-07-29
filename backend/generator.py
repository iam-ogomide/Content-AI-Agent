import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai

load_dotenv()

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client = None


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


def build_prompt(topic, channel, tone, audience=None, cta=None, keyword=None):
    brand_voice = _load_brand_voice()

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
    brief = "\n".join(brief_lines)

    return f"""You are the Generator Agent for CreditChek's marketing team.
Write a first-draft piece of content strictly following the brand voice doc below.

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---

CONTENT BRIEF:
{brief}

Instructions:
- Write only the content itself. No preamble, no explanation, no markdown headers.
- Match the tone and channel conventions described in the brand voice doc.
- If a CTA is given, end with it clearly.
- Respect the target channel's typical length and format (e.g. X/Twitter is short, LinkedIn allows more room, blog can be longer-form).
"""


def generate_draft(topic, channel, tone, audience=None, cta=None, keyword=None):
    if not topic or not channel or not tone:
        raise ValueError("topic, channel, and tone are required")

    client = _get_client()
    prompt = build_prompt(topic, channel, tone, audience, cta, keyword)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    return response.text
