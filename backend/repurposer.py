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


FORMAT_GUIDANCE = {
    "LinkedIn post": "A single LinkedIn post. Keep the strongest hook from the source as the opening line.",
    "X/Twitter thread": "A numbered tweet thread (each tweet under 280 characters, prefixed 'N/'). 3-6 tweets.",
    "Instagram caption": "A warm, casual Instagram caption. Include 3-5 relevant hashtags at the end.",
    "Email summary": "A short email with a benefit-first subject line (prefixed 'Subject:') followed by a 2-4 paragraph body and one clear CTA.",
    "Carousel copy": "Copy for a slide carousel. Output each slide as 'Slide N: <text>' on its own line, 5-8 slides, one idea per slide.",
    "Quote card": "A single short, punchy quote (under 25 words) pulled or adapted from the source, suitable for a visual quote card.",
}


def build_prompt(source_content, target_format, tone_shift=None, word_limit=None):
    brand_voice = _load_brand_voice()
    guidance = FORMAT_GUIDANCE.get(target_format, f"Content formatted as: {target_format}")

    constraints = [f"Target format: {target_format} — {guidance}"]
    if tone_shift:
        constraints.append(f"Tone shift requested: {tone_shift}")
    if word_limit:
        constraints.append(f"Word limit: {word_limit} words")
    constraints_block = "\n".join(f"- {c}" for c in constraints)

    return f"""You are the Repurposer Agent for CreditChek's marketing team.
Take the source content below and repurpose it into the target format, preserving its core message while adapting structure and length. Follow the brand voice doc.

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---

SOURCE CONTENT:
---
{source_content}
---

REPURPOSING INSTRUCTIONS:
{constraints_block}

Output only the repurposed content itself. No preamble, no explanation, no markdown headers.

Section 3 of the doc ("one person, one problem, one solution") is non-negotiable. Follow it as
written, including its exception for source content that is explicitly market-level.
"""


def repurpose_content(source_content, target_format, tone_shift=None, word_limit=None):
    if not source_content or not target_format:
        raise ValueError("source_content and target_format are required")

    client = _get_client()
    prompt = build_prompt(source_content, target_format, tone_shift, word_limit)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    return response.text


def repurpose_content_stream(source_content, target_format, tone_shift=None, word_limit=None):
    """Same call as repurpose_content, but yields text as Gemini streams it back."""
    if not source_content or not target_format:
        raise ValueError("source_content and target_format are required")

    client = _get_client()
    prompt = build_prompt(source_content, target_format, tone_shift, word_limit)

    for chunk in client.models.generate_content_stream(model=MODEL_NAME, contents=prompt):
        if chunk.text:
            yield chunk.text
