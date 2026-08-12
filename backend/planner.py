"""Planner Agent — builds a content calendar from the brand voice doc.

Unlike the other agents, this one returns data rather than prose: a list of
slots, each of which is a ready-to-use Generator brief (topic, channel, tone).
The forced schema is what makes that safe to consume programmatically.

It deliberately stops at the calendar and does not draft anything. A calendar is
a decision document — the point is for a human to read two weeks of planned
content and rebalance it before any drafting happens. Drafting a slot is a
separate request ("write the LinkedIn post for August 4th").
"""

import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from product_context import load_product_context
from tracing import model_span

load_dotenv()

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

_client = None

# The channels a slot may be scheduled on. Same list the rest of the system
# uses, so a slot can be handed straight to the Generator.
CHANNELS = ["LinkedIn", "X (Twitter)", "Instagram", "Email", "Blog"]

# Section 6 of the brand voice doc. Kept as an enum so a plan can't invent a
# pillar the content strategy doesn't have.
PILLARS = [
    "Product Education",
    "Industry Insights",
    "Problem Awareness",
    "Customer Success & Proof",
    "Thought Leadership & Vision",
    "Community & Engagement",
]

# Section 4's ICPs. Optional per slot — not every piece targets one.
ICPS = [
    "Fintechs & Digital Lenders",
    "Microfinance Institutions & SACCOs",
    "E-commerce & BNPL Providers",
    "Aggregators & Platforms (B2B2C)",
    "Banks & Traditional Financial Institutions",
    "B2C (CreditCliq / ReboundCliq)",
]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "timeframe": {"type": "string"},
        "slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "channel": {"type": "string", "enum": CHANNELS},
                    "pillar": {"type": "string", "enum": PILLARS},
                    "topic": {"type": "string"},
                    "angle": {"type": "string"},
                    "tone": {"type": "string"},
                    "icp": {"type": "string", "enum": ICPS},
                    "cta": {"type": "string"},
                },
                "required": ["date", "channel", "pillar", "topic", "angle", "tone"],
            },
        },
        "coverage_notes": {"type": "string"},
        "cadence_notes": {"type": "string"},
    },
    "required": ["timeframe", "slots", "coverage_notes", "cadence_notes"],
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


def build_prompt(timeframe, channels, pillars=None, theme=None, icp=None,
                 posts_per_week=None, today=None):
    brand_voice = _load_brand_voice()

    # brand_voice.md sections 1/4/9 (referenced in the rules below) only carry
    # a short product summary; product.pdf is the internal handover doc behind
    # it — real per-product status and specifics to draw topics from instead
    # of inventing angles. Optional: an empty string just drops the block.
    product_context = load_product_context()
    product_block = ""
    if product_context:
        product_block = f"""
--- PRODUCT CONTEXT (internal handover doc) ---
{product_context}
--- END PRODUCT CONTEXT ---

Use this doc to ground topics and angles in what CreditChek actually ships, and to get
product names, capabilities, and status (live / shipping / planned / in development) right.
Never schedule a slot that presents a planned or in-development item as available now — if a
slot leans on one, say so in its angle. This doc is for factual grounding only: it contains
internal details (contacts, emails, roadmap risk notes) that must never appear in a slot's
topic, angle, or notes, and must never be quoted from directly or mentioned as a source.
"""

    # The model has no reliable sense of the current date, so "next 2 weeks"
    # would otherwise resolve to whenever its training data ended. Anchor it.
    today = today or date.today().isoformat()

    brief_lines = [
        f"Timeframe: {timeframe}",
        f"Channels to cover: {', '.join(channels)}",
    ]
    if pillars:
        brief_lines.append(f"Emphasize these pillars: {', '.join(pillars)}")
    if theme:
        brief_lines.append(f"Campaign / theme to build around: {theme}")
    if icp:
        brief_lines.append(f"Primary ICP to target: {icp}")
    if posts_per_week:
        brief_lines.append(
            f"Posts per week: {posts_per_week} (overrides the doc's default cadence)"
        )
    brief = "\n".join(brief_lines)

    return f"""You are the Planner Agent for CreditChek's marketing team.
Build a content calendar. You do not write the content itself — each slot is a brief
that the Generator Agent will draft later.

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---
{product_block}
TODAY'S DATE: {today}

PLANNING BRIEF:
{brief}

Rules:
- Use ONLY the channels listed in the brief above. This is an exclusive list, not a
  suggestion — a plan for LinkedIn and Email must contain no Blog, X, or Instagram slots.
  If a strong idea belongs on a channel that was not requested, mention it in
  coverage_notes rather than scheduling it.
- Resolve the timeframe against today's date above. Every slot needs a real calendar
  date in YYYY-MM-DD format, in chronological order.
- Follow the per-channel cadence in section 8 of the doc unless the brief overrides it
  (LinkedIn 3-4/week, Blog 3 articles/month, Email newsletter bi-weekly). These are rates
  per week or per month, not totals for the whole timeframe — multiply them out before you
  start. Work out the expected slot count for each requested channel first: two weeks of
  LinkedIn at 3-4/week is 6-8 slots, not 3-4. Fill that count. Under-filling a channel is
  as wrong as overfilling it.
- X/Twitter is the exception: the doc's 5-7 daily conversational tweets are not worth
  planning individually. Schedule 2-3 substantive X slots per week — threads and anchor
  posts — and say in cadence_notes that day-to-day conversational tweeting sits outside
  this calendar. A calendar of 70 tweet slots is not a usable planning document.
- Draw topics from the six content pillars in section 6 and spread them across the
  timeframe. Do not let one pillar dominate — section 6 exists so the calendar is not
  all product promotion. Community & Engagement in particular means participating in the
  ecosystem, not self-promoting.
- Ground topics in real CreditChek products, proof points, and narrative hooks from the
  doc (sections 1, 4, 9). Prefer the recurring hooks in section 4 over invented angles.
- Respect section 9: proof points are real but age. If a slot leans on a figure, say so
  in the angle so it can be re-verified before publishing.
- topic is what the piece is about, in a phrase the Generator can work from. angle is the
  specific take or hook — the reason this piece is worth publishing rather than a generic
  post on the subject.
- tone should suit the channel and pillar. Section 2's tonal duality applies: the warmer
  register belongs to Email newsletters, the formal one everywhere else.
- Set icp only when a slot clearly targets one of section 4's profiles. Vary it across
  the calendar rather than aiming everything at the same reader.
- cta is optional per slot, but a piece with no CTA should be a deliberate choice.

coverage_notes: 2-3 sentences on how the plan balances pillars, channels, and ICPs — and
what it deliberately left out.
cadence_notes: one sentence on how the schedule maps to the doc's cadence, and anywhere
you departed from it and why.
"""


def generate_plan(timeframe, channels, pillars=None, theme=None, icp=None,
                  posts_per_week=None, today=None):
    if not timeframe or not channels:
        raise ValueError("timeframe and channels are required")

    if isinstance(channels, str):
        channels = [channels]

    client = _get_client()
    prompt = build_prompt(timeframe, channels, pillars, theme, icp, posts_per_week, today)

    with model_span("generate_plan", prompt, MODEL_NAME) as span:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PLAN_SCHEMA,
            ),
        )
        span.record(response)
    return json.loads(response.text)
