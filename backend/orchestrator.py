"""Orchestrator Agent — routes plain-English requests to the right sub-agents.

This is the brain, it never writes content itself. Its job is four things:

  1. Parse intent from a plain-English message into a structured brief.
  2. Decide which sub-agent(s) to run, and in what order.
  3. Manage conversation memory so follow-ups ("make it shorter") work.
  4. Assemble one coherent response from however many agents ran.

Sub-agents declare themselves in the AGENTS registry below, so adding the
Planner later is a registry entry rather than a rewrite of the routing logic.
"""

import json
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from generator import generate_draft, generate_draft_stream
from planner import ICPS, PILLARS, generate_plan
from repurposer import repurpose_content, repurpose_content_stream
from reviewer import review_draft

load_dotenv()

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client = None

CHANNELS = ["LinkedIn", "X (Twitter)", "Instagram", "Email", "Blog", "Graphic"]


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or api_key == "your-gemini-api-key-here":
            raise RuntimeError("GEMINI_API_KEY is not set. Add your real key to .env")
        _client = genai.Client(api_key=api_key)
    return _client


# --------------------------------------------------------------------------
# Sub-agent registry
# --------------------------------------------------------------------------
# This encodes ("What Each Agent Needs") as data.
#   required / optional — which params the agent takes. Missing a required one
#                         means asking the user instead of erroring.
#   consumes            — the param this agent can fill from the previous
#                         step's output, which is what makes chaining generic.
#   produces            — the key its result is stored under.

AGENTS = {
    "generate": {
        "fn": generate_draft,
        "stream_fn": generate_draft_stream,
        "label": "Generator",
        "required": ["topic", "channel", "tone"],
        "optional": [
            "audience", "cta", "keyword", "word_limit",
            "previous_draft", "revision_note",
        ],
        "consumes": None,
        "produces": "draft",
    },
    "plan": {
        "fn": generate_plan,
        "label": "Planner",
        "required": ["timeframe", "channels"],
        "optional": ["pillars", "theme", "icp", "posts_per_week"],
        # A calendar is a chain starter, and it deliberately does not feed the
        # Generator: 12 slots would mean 12 drafts of a plan the user has not
        # read yet. Drafting a slot is a separate request.
        "consumes": None,
        # Not "plan": result["plan"] already holds the routed intent list, and
        # this would silently overwrite it.
        "produces": "calendar",
    },
    "repurpose": {
        "fn": repurpose_content,
        "stream_fn": repurpose_content_stream,
        "label": "Repurposer",
        "required": ["source_content", "target_format"],
        "optional": ["tone_shift", "word_limit"],
        "consumes": "source_content",
        "produces": "repurposed",
        # This agent names its inputs differently from the rest of the system:
        # its "source_content" is what everyone else calls the draft. Map the
        # param onto the brief key that holds it rather than renaming either.
        "aliases": {"source_content": "draft"},
    },
    "review": {
        "fn": review_draft,
        "label": "Reviewer",
        "required": ["draft", "channel"],
        "optional": [],
        "consumes": "draft",
        "produces": "report",
        # Several brand rules are conditional on what the piece was asked to be.
        # The Reviewer can't see that from prose alone, so hand it the brief.
        "wants_brief": True,
    },
}

# Repurpose targets, and the channel each one lands on so a follow-up review
# scores it against the right conventions.
FORMAT_TO_CHANNEL = {
    "LinkedIn post": "LinkedIn",
    "X/Twitter thread": "X (Twitter)",
    "Instagram caption": "Instagram",
    "Email summary": "Email",
    "Carousel copy": "Instagram",
    "Quote card": "Instagram",
}
TARGET_FORMATS = list(FORMAT_TO_CHANNEL)

# Brief keys that describe the *intent* of a piece, as opposed to its content or
# the mechanics of a revision. Only these are worth showing the Reviewer.
BRIEF_INTENT_KEYS = ["topic", "channel", "tone", "audience", "cta", "keyword", "word_limit"]

# Intents the router may return but that aren't built yet. All four agents in
# the brief are live; this stays as the hook for whatever comes next.
NOT_BUILT = {}

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["generate", "review", "repurpose", "plan", "unknown"],
            },
        },
        "topic": {"type": "string"},
        "channel": {"type": "string", "enum": CHANNELS},
        "tone": {"type": "string"},
        "audience": {"type": "string"},
        "cta": {"type": "string"},
        "keyword": {"type": "string"},
        "word_limit": {"type": "integer"},
        "draft": {"type": "string"},
        "target_format": {"type": "string", "enum": TARGET_FORMATS},
        "tone_shift": {"type": "string"},
        "timeframe": {"type": "string"},
        "channels": {"type": "array", "items": {"type": "string", "enum": CHANNELS}},
        "pillars": {"type": "array", "items": {"type": "string", "enum": PILLARS}},
        "theme": {"type": "string"},
        "icp": {"type": "string", "enum": ICPS},
        "posts_per_week": {"type": "integer"},
        "revision_note": {"type": "string"},
        "auto_revise": {"type": "boolean"},
        "is_followup": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["intents", "is_followup", "reasoning"],
}

ROUTER_PROMPT = """You are the Orchestrator Agent for CreditChek's marketing content system.
You do not write content. You read the user's message and decide which sub-agents should run.

AVAILABLE INTENTS:
- generate: the user wants new content written from a brief.
- review: the user wants existing content scored against brand guidelines.
- repurpose: the user wants existing content reformatted for a different channel.
- plan: the user wants a content calendar built.
- unknown: the message is not a content request (a greeting, an unrelated question).

CHAINING: return multiple intents in the order they should run. "Write a LinkedIn post
and check the tone" is ["generate", "review"]. Reviewing what was just generated is a
chain, not two separate requests. "Write a blog post and turn it into a thread" is
["generate", "repurpose"]. Repurposing the last draft is a repurpose on its own — do not
re-run generate unless the user asks for new content.

EXTRACTION RULES:
- channel must be exactly one of: {channels}. Map what the user says onto these
  ("twitter"/"tweet" -> "X (Twitter)", "IG"/"insta" -> "Instagram", "newsletter" -> "Email",
  "graphic copy"/"ad graphic"/"design copy"/"headline and CTA" -> "Graphic"). "Graphic" produces
  a headline, supporting text, and a CTA only — no design — for handoff to a designer.
  If no channel is stated or implied, omit the field entirely.
- If a generate request names MORE THAN ONE channel in the same message ("a LinkedIn post
  and an Instagram caption about X", "write this for LinkedIn and X too", "a full campaign
  with a LinkedIn post, an Instagram caption, an X post, and graphic copy"), put every channel
  in `channels` as a list and omit `channel` entirely. Each one gets its own draft, all built
  from the same topic/tone/audience/cta/keyword stated in this message. Use `channel`
  (singular) whenever exactly one channel applies — including a follow-up that adds just one
  more ("now do Instagram too"), which stays a single `channel`, not a one-item `channels` list.
- topic: the concrete subject the content should be about — a specific product, feature,
  story, or angle. If the message names or lists candidate products/features and leaves the
  choice to you ("around one CreditChek product", "pick one of our products", a message that
  names several products in passing without picking one), DO NOT leave topic blank and do not
  ask which one — pick the single best-fitting candidate yourself (favor the one the rest of
  the message's audience/angle points at) and set topic to that specific product by name, e.g.
  "Income Insight". Say which one you picked and why in reasoning. Only omit topic entirely
  when the message gives you nothing to build from at all — no product named or listed, no
  feature, no story, no angle. This matters most on a multi-channel request: every channel is
  generated independently, so an unresolved topic lets each one land on a different product
  and the "campaign" loses its one core message — resolving it to one concrete product here is
  what keeps every channel consistent.
- tone: only if the user describes one. Do not invent a tone.
- word_limit: an integer, only if the user states a length ("under 200 words" -> 200).
  For character limits on X, leave word_limit unset; the channel handles that.
- draft: only fill this if the user pasted actual content to be reviewed or repurposed.
  Do not put a description of content here.
- target_format: for repurpose requests only, exactly one of: {formats}. Map what the user
  says onto these ("thread" -> "X/Twitter thread", "carousel" -> "Carousel copy",
  "quote graphic" -> "Quote card", "newsletter version" -> "Email summary"). Omit it if the
  user asked to repurpose but did not say into what.
- tone_shift: for repurpose requests, only if the user asks for a different tone than the
  source ("make it more casual"). Use tone_shift here, not tone.
- For plan requests only: timeframe is the span in the user's own words ("next 2 weeks",
  "August", "Q4"). channels is a LIST of every channel the calendar should cover — use
  `channels`, not `channel`, and include all of them. This applies even to a short
  follow-up naming just one channel ("linkedin" in answer to "which channels should it
  cover?") — put it in `channels` as a single-item list, never in `channel`. If the user
  says "everything" or names no channel, omit the field and it will be asked for.
- pillars, theme, icp, posts_per_week: plan requests ONLY — never populate these for a
  "generate" intent, even one covering several channels. The word "campaign" is ambiguous in
  casual use: a message calling for a calendar of future posts is a plan request (these fields
  apply), but a message that asks you to write specific pieces right now — "a LinkedIn post, an
  Instagram caption, an X post" — is a multi-channel generate request even if the user calls it
  a "campaign". For that kind of request, treat it exactly like any other generate request:
  extract tone/audience/cta/keyword from what the message actually says, and do not reach for
  pillars/theme/icp/posts_per_week instead of them.
  Example: "Write a LinkedIn post and an Instagram caption about Income Insight for lenders,
  professional tone." -> intents ["generate"], topic "Income Insight",
  channels ["LinkedIn", "Instagram"], tone "professional", audience "lenders" (plus
  is_followup/reasoning). Nothing else — no pillars, theme, icp, or posts_per_week, and
  tone/audience are populated exactly as they would be for a single-channel request; having
  more than one channel changes nothing about how those two fields are read.
- Omit any field the user did not give you. Do not guess or fill in defaults.

AUTO-REVISE: set auto_revise true only when the user asks for content to be brought up
to standard without further input from them — "polish it", "make sure it's good",
"keep working on it until it passes", "write it and fix any issues". This runs
generate -> review -> revise repeatedly until the draft passes, so it costs several
model calls. Do NOT set it for a plain "write X and review it": that means the user
wants to see the score and decide for themselves. When auto_revise is true, intents
should be ["generate", "review"].

FOLLOW-UPS: set is_followup true when the message modifies a previous request rather
than starting a new one ("make it shorter", "try a warmer tone", "now do one for X").
On a follow-up, only extract the fields the user is actually changing.

ANSWERING A CLARIFYING QUESTION: if the last agent turn in CONVERSATION SO FAR ends in a
question (it asked for a missing topic, tone, channel, timeframe, etc.), treat this message
as completing that same request rather than a new or unclear one — even when it is a single
bare word or phrase with no other context ("professional", "LinkedIn", "our new savings
product", "next 2 weeks"). Set is_followup true, reuse the same intents the previous turn
was building toward (visible from what was asked and what the brief so far is missing —
usually ["generate"]), and extract the field(s) this message answers. Only fall back to
"unknown" when the message neither answers the pending question nor reads as any kind of
content request.

REVISION NOTES: on a follow-up asking to change existing content, put the change in
revision_note as a short instruction ("make it shorter", "cut the mention of Africa",
"punchier opening"). Rules:
- A revision is ALWAYS the "generate" intent, never "review". Rewriting a draft is
  generation work. Only use "review" when the user asks for feedback, a score, or an
  opinion on a draft — not when they ask for it to be changed.
- Relative length requests must ALSO set a concrete word_limit, because the agent
  cannot act on "shorter" alone. The previous draft's length is given below when one
  exists: "shorter" is roughly 60% of it, "much shorter" roughly 40%, "a bit shorter"
  roughly 80%. Round to a whole number.
- A request that changes tone belongs in BOTH tone and revision_note.
- Do not set revision_note when the user is answering a question you asked (supplying
  a missing channel or tone) — that is completing a brief, not revising a draft.

reasoning: one short sentence on why you picked these intents.

{history_block}{draft_block}
USER MESSAGE:
{message}
"""


# --------------------------------------------------------------------------
# Session memory
# --------------------------------------------------------------------------
# Conversation memory only — turns plus the running brief, so follow-ups
# resolve. The vector-DB knowledge layer in section 5 of the brief is a
# separate concern (phase 4).
#
# Sessions are stored in MongoDB (collection `content_ai_sessions`), not an
# in-process dict or file, so a sidebar listing past conversations survives a
# container rebuild and multiple gunicorn workers share one source of truth
# instead of clobbering each other's writes. A session document is fetched
# once at the start of a turn, mutated in place as the turn runs, and written
# back once at the end. Every read and write goes through the accessors below.

MAX_HISTORY = 6

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
SESSIONS_COLLECTION_NAME = "content_ai_sessions"

_mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
_sessions_collection = (
    _mongo_client[MONGO_DB][SESSIONS_COLLECTION_NAME] if _mongo_client is not None else None
)

# What the frontend needs to redraw a past turn. `text` is always present and is
# all the router reads (see _history_block); these are extra keys alongside it,
# so widening this list stays invisible to routing.
ARTIFACT_KEYS = ("ran", "draft", "repurposed", "report", "calendar", "channel", "drafts", "reports")

# How many characters of the opening message become a conversation's title.
TITLE_LENGTH = 60


def _new_session():
    # `created` orders the sidebar; `updated` is what actually sorts it, so a
    # revived old conversation rises back to the top.
    stamp = datetime.now(timezone.utc).isoformat()
    return {"history": [], "brief": {}, "last_draft": None,
            "title": "", "created": stamp, "updated": stamp}


def _get_session(session_id):
    doc = _sessions_collection.find_one({"_id": session_id}) if _sessions_collection is not None else None
    if doc is None:
        return _new_session()
    doc.pop("_id", None)
    return {**_new_session(), **doc}


def _save_store(session_id, session):
    """Persist one session document. Best-effort: a failed write must never
    break a turn that already cost a Gemini call, so the error is swallowed.
    """
    if _sessions_collection is None:
        return
    try:
        _sessions_collection.replace_one({"_id": session_id}, session, upsert=True)
    except PyMongoError:
        pass


def reset_session(session_id):
    if _sessions_collection is None:
        return
    try:
        _sessions_collection.delete_one({"_id": session_id})
    except PyMongoError:
        pass


def get_history(session_id):
    """The full stored history for a session, oldest turn first.

    Deliberately not truncated to MAX_HISTORY: that bounds how much context the
    router is given, not how far back a user may scroll.
    """
    if _sessions_collection is None:
        return []
    doc = _sessions_collection.find_one({"_id": session_id}, {"history": 1})
    return doc["history"] if doc else []


def list_sessions():
    """Every stored conversation as a sidebar entry, most recently used first.

    Returns metadata only — no history, no drafts. The sidebar renders dozens of
    these, and shipping every draft with them would send the whole store on
    every page load.
    """
    if _sessions_collection is None:
        return []
    pipeline = [
        {"$match": {"history.0": {"$exists": True}}},  # a session opened but never used
        {"$project": {
            "title": 1, "created": 1, "updated": 1,
            "turns": {"$size": {"$ifNull": ["$history", []]}},
        }},
        {"$sort": {"updated": -1}},
    ]
    return [
        {
            "session_id": doc["_id"],
            "title": doc.get("title") or "New conversation",
            "created": doc.get("created", ""),
            "updated": doc.get("updated", ""),
            "turns": doc.get("turns", 0),
        }
        for doc in _sessions_collection.aggregate(pipeline)
    ]


def _touch_session(session, message):
    """Stamp a session as just-used, titling it from its first message.

    The title comes from the opening message only — later messages are usually
    follow-ups ("make it shorter"), which describe the conversation far worse
    than what started it.
    """
    session["updated"] = datetime.now(timezone.utc).isoformat()
    if not session.get("title") and message:
        title = " ".join(message.split())
        session["title"] = (
            title if len(title) <= TITLE_LENGTH else title[:TITLE_LENGTH].rstrip() + "…"
        )


def _record_agent_turn(session_id, session, result):
    """Store an agent turn as the same object shape the frontend renders live,
    so replaying history needs no separate rendering path.

    The reply line alone is not enough to redraw a turn — it says "Looks good —
    90/100" without the draft or the score breakdown — so the artifacts a turn
    produced are stored beside it.
    """
    turn = {"role": "agent", "text": result.get("reply", "")}
    for key in ARTIFACT_KEYS:
        if result.get(key):
            turn[key] = result[key]
    session["history"].append(turn)

    # The turn is complete here — every exit path records an agent turn — so
    # this is the one place that has to persist.
    _save_store(session_id, session)


def _history_block(session):
    if not session["history"]:
        return ""
    turns = "\n".join(f"{t['role']}: {t['text']}" for t in session["history"][-MAX_HISTORY:])
    return f"CONVERSATION SO FAR:\n{turns}\n"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

# The router reliably confuses a multi-channel generate ("a LinkedIn post, an
# Instagram caption, and an X post") with a plan/calendar request once it sees
# more than one channel — even with explicit rules and a worked example telling
# it not to — and fills these plan-only fields while dropping tone/audience
# instead of extracting them. Rather than keep fighting the model's prior in
# prose, strip these deterministically whenever "plan" isn't one of the
# returned intents, since AGENTS["generate"] never consumes them anyway.
PLAN_ONLY_FIELDS = ("pillars", "theme", "icp", "posts_per_week", "timeframe")

# Same failure mode's other half: tone is a required field for "generate", so
# losing it to the same confusion forces a clarifying question the user has
# already answered in plain text ("...professional tone."). A small regex
# catches the common "<word> tone" phrasing the router keeps dropping, without
# a second model call. Words that name the tone belong right before "tone";
# these are the fillers that would land there without actually naming one.
_TONE_STOPWORDS = {
    "the", "a", "an", "this", "that", "same", "right", "correct",
    "appropriate", "usual", "similar", "matching", "consistent",
}
_TONE_RE = re.compile(r"\b([a-zA-Z][a-zA-Z-]*)\s+tone\b", re.IGNORECASE)


def _fallback_tone(message):
    for match in _TONE_RE.finditer(message):
        word = match.group(1).lower()
        if word not in _TONE_STOPWORDS:
            return word
    return None


def route(message, session=None):
    """Turn a plain-English message into a plan plus extracted params."""
    client = _get_client()
    history_block = _history_block(session) if session else ""

    # Give the router the current draft's length so it can turn a relative
    # request ("shorter") into a concrete word_limit.
    draft_block = ""
    if session and session.get("last_draft"):
        words = len(session["last_draft"].split())
        draft_block = f"PREVIOUS DRAFT LENGTH: {words} words\n"

    prompt = ROUTER_PROMPT.format(
        channels=", ".join(f'"{c}"' for c in CHANNELS),
        formats=", ".join(f'"{f}"' for f in TARGET_FORMATS),
        history_block=history_block,
        draft_block=draft_block,
        message=message,
    )

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ROUTE_SCHEMA,
            # This is classification/extraction, not creative writing — the
            # default temperature let identical follow-ups ("professional"
            # answering a tone question) sometimes route correctly and
            # sometimes come back as a fresh, context-free request, wiping
            # the brief built up so far. Low temperature makes the same input
            # route the same way.
            temperature=0.1,
        ),
    )
    routed = json.loads(response.text)

    if "plan" not in routed.get("intents", []):
        for field in PLAN_ONLY_FIELDS:
            routed.pop(field, None)

    if not routed.get("tone"):
        guess = _fallback_tone(message)
        if guess:
            routed["tone"] = guess

    # Strip the empty strings the schema allows so they don't overwrite
    # remembered values when merged into the session brief.
    return {k: v for k, v in routed.items() if v != "" and v is not None}


def _resolve_params(intent, spec, brief, chain_output):
    """Collect an agent's args from the chain, then the running brief."""
    params = {}
    missing = []

    aliases = spec.get("aliases", {})

    for name in spec["required"] + spec["optional"]:
        value = None
        brief_key = aliases.get(name, name)

        # A chained value takes precedence: reviewing the draft that was just
        # generated should use that draft, not a stale one from the session.
        if name == spec["consumes"] and chain_output is not None:
            value = chain_output
        elif brief_key in brief:
            value = brief[brief_key]

        if value is not None:
            params[name] = value
        elif name in spec["required"]:
            missing.append(name)

    # Agents that judge a draft need to know what was asked for, not just what
    # was produced — a rule with a stated exception can't resolve without it.
    if spec.get("wants_brief"):
        intent_brief = {k: brief[k] for k in BRIEF_INTENT_KEYS if brief.get(k)}
        if intent_brief:
            params["brief"] = intent_brief

    return params, missing


QUESTIONS = {
    "topic": "What should the content be about?",
    "channel": f"Which channel? ({', '.join(CHANNELS)})",
    "tone": "What tone should it take?",
    "draft": "Paste the draft you want reviewed.",
    "source_content": "Paste the content you want repurposed.",
    "target_format": f"What should I repurpose it into? ({', '.join(TARGET_FORMATS)})",
    "timeframe": "What period should the calendar cover? (e.g. next 2 weeks, August)",
    "channels": f"Which channels should it cover? ({', '.join(CHANNELS)})",
}

# Cap on the generate -> review -> revise cycle. Each extra attempt is two more
# model calls, and in practice a draft that fails three times needs a human
# rather than another attempt.
MAX_REVISE_ATTEMPTS = 3


def _revision_note_from(report):
    """Turn a review report into an instruction the Generator can act on."""
    parts = [report.get("summary", "").strip()]

    # Category notes are more specific than the summary, so include the ones
    # that actually failed.
    for key in ("tone", "clarity", "cta_strength", "grammar", "seo_basics"):
        category = report.get(key) or {}
        if isinstance(category, dict) and category.get("score", 100) < 75:
            note = (category.get("notes") or "").strip()
            if note:
                parts.append(f"{key.replace('_', ' ')}: {note}")

    return " ".join(p for p in parts if p)


def _stream_text_agent(field, stream_fn, params, channel=None, label=None):
    """Run a streaming Generator/Repurposer call, yielding one event per chunk
    as Gemini streams it back, and returning the full text once done.

    Callers drive this with `yield from` so the events surface all the way up
    to the HTTP response while the accumulated text comes back as this
    generator's return value — the same shape as a plain function call.

    `label` overrides the frontend's default card title (just "Draft") — used
    when several drafts stream in the same turn and need to read as distinct
    cards, e.g. "Draft — LinkedIn" next to "Draft — Instagram".
    """
    chunks = []
    yield {"type": "text_start", "field": field, "channel": channel, "label": label}
    for chunk in stream_fn(**params):
        chunks.append(chunk)
        yield {"type": "text_chunk", "field": field, "text": chunk}
    text = "".join(chunks)
    yield {"type": "text_done", "field": field, "text": text}
    return text


def _auto_revise_stream(generate_spec, review_spec, gen_params, channel, brief=None, field="draft", label=None):
    """Streaming counterpart to the generate -> review -> revise loop: each
    Generator call streams its text live; the Reviewer still runs as one
    blocking call per attempt, since a partial score isn't meaningful.

    This is the one genuinely agentic loop in the system: the Reviewer's verdict
    drives another Generator call with no user input. It is bounded on purpose.

    The brief goes to the Reviewer as well as the Generator. Without it the loop
    can spend every attempt chasing a rule the brief exempted the piece from.

    `field`/`label` are only overridden by the multi-channel caller, which runs
    this loop once per channel and needs each one to land on its own card.
    """
    attempts = []
    review_kwargs = {"brief": brief} if brief else {}

    draft = yield from _stream_text_agent(field, generate_spec["stream_fn"], gen_params, channel, label)

    for _ in range(MAX_REVISE_ATTEMPTS):
        report = review_spec["fn"](draft=draft, channel=channel, **review_kwargs)
        attempts.append({"score": report.get("overall_score"), "verdict": report.get("verdict")})

        if report.get("verdict") == "pass":
            return draft, report, attempts

        note = _revision_note_from(report)
        if not note:
            return draft, report, attempts

        revise_params = {**gen_params, "previous_draft": draft, "revision_note": note}
        draft = yield from _stream_text_agent(field, generate_spec["stream_fn"], revise_params, channel, label)

    # Out of attempts: score the final revision so the caller reports the draft
    # it is actually returning, not the previous one's verdict.
    report = review_spec["fn"](draft=draft, channel=channel, **review_kwargs)
    attempts.append({"score": report.get("overall_score"), "verdict": report.get("verdict")})
    return draft, report, attempts


# Multi-channel generate is handled as its own branch rather than folded into
# the generic chain loop below: a calendar's `channels` list describes one
# plan spanning several channels, but a generate request naming several
# channels means N unrelated pieces, each needing its own stream, its own
# review (if asked for), and its own card. repurpose/plan don't have a
# multi-channel shape, so this only ever fires for generate (+ review).
MULTI_CHANNEL_INTENTS = {"generate", "review"}


def _run_multi_channel_stream(channels, do_review, auto_revise, brief, session, result, session_id):
    """Run generate (and optionally review/auto-revise) once per channel named
    in a single message, streaming each draft to its own card.

    Mirrors the single-channel branches below, just looped — kept separate
    from them because threading a channel list through the generic chain loop
    (which assumes one `chain_output` per step) would make that loop harder to
    follow for the common single-channel case it mostly serves.
    """
    gen_spec = AGENTS["generate"]
    review_spec = AGENTS["review"]

    # topic/tone are shared across every channel in this request, so one probe
    # (using the first channel just to satisfy the schema) is enough to catch
    # what's missing rather than repeating the same question per channel.
    _, missing = _resolve_params("generate", gen_spec, {**brief, "channel": channels[0]}, None)
    if missing:
        asked = [QUESTIONS.get(m, f"I need a value for {m}.") for m in missing]
        result["reply"] = " ".join(asked)
        result["needs"] = missing
        _record_agent_turn(session_id, session, result)
        yield {"type": "result", **result}
        return

    intent_brief = {k: brief[k] for k in BRIEF_INTENT_KEYS if brief.get(k)}
    intent_brief.pop("channel", None)

    drafts = []
    reports = []
    reply_parts = []

    for channel in channels:
        gen_params, _ = _resolve_params("generate", gen_spec, {**brief, "channel": channel}, None)
        field = f"draft:{channel}"
        label = f"Draft — {channel}"
        channel_brief = {**intent_brief, "channel": channel} if do_review else None

        if auto_revise and do_review:
            draft, report, attempts = yield from _auto_revise_stream(
                gen_spec, review_spec, gen_params, channel, channel_brief, field=field, label=label
            )
            reports.append({"channel": channel, "report": report, "attempts": attempts})
        elif do_review:
            draft = yield from _stream_text_agent(field, gen_spec["stream_fn"], gen_params, channel, label)
            report = review_spec["fn"](draft=draft, channel=channel, brief=channel_brief)
            reports.append({"channel": channel, "report": report})
        else:
            draft = yield from _stream_text_agent(field, gen_spec["stream_fn"], gen_params, channel, label)

        drafts.append({"channel": channel, "draft": draft})

        if do_review:
            report = reports[-1]["report"]
            verdict = "Looks good" if report.get("verdict") == "pass" else "Needs work"
            reply_parts.append(f"{channel}: {verdict} — {report.get('overall_score')}/100.")
        else:
            reply_parts.append(f"{channel}: draft ready.")

    session["last_draft"] = drafts[-1]["draft"]
    brief["draft"] = drafts[-1]["draft"]

    result["drafts"] = drafts
    result["ran"] = ["generate", "review"] if do_review else ["generate"]
    if do_review:
        result["reports"] = reports
    if auto_revise:
        result["auto_revised"] = True
    lead = f"Built this around {brief['topic']}. " if brief.get("topic") else ""
    result["reply"] = lead + " ".join(reply_parts)
    _record_agent_turn(session_id, session, result)
    yield {"type": "result", **result}


def _handle_message_events(message, session_id="default"):
    """Generator core for a full turn: route, execute the plan, assemble one
    response — yielding streaming events for any Generator/Repurposer call
    along the way, and a final {"type": "result", ...} event carrying exactly
    what `handle_message` used to return outright.

    `handle_message` drains this and returns just the result, so existing
    callers see no difference; `handle_message_stream` is the same generator
    exposed directly for a caller (the Flask route) that wants the events.
    """
    if not message or not message.strip():
        raise ValueError("message is required")

    message = message.strip()
    session = _get_session(session_id)

    routed = route(message, session)
    intents = routed.pop("intents", [])
    is_followup = routed.pop("is_followup", False)
    reasoning = routed.pop("reasoning", "")
    auto_revise = routed.pop("auto_revise", False)

    # A bare follow-up naming one channel ("linkedin") sometimes lands in the
    # singular `channel` slot instead of the plural `channels` a plan needs.
    # Left alone, `channels` never fills and the same question repeats forever.
    # Safe to correct only when nothing else in this turn wants the singular
    # field.
    if (
        "plan" in intents
        and "generate" not in intents
        and "review" not in intents
        and "channels" not in routed
        and "channel" in routed
    ):
        routed["channels"] = [routed.pop("channel")]

    # Whatever is left in `routed` is extracted brief params. On a follow-up we
    # layer them over the remembered brief; on a new request they replace it.
    if is_followup:
        session["brief"].update(routed)
    else:
        session["brief"] = dict(routed)
    brief = session["brief"]

    # A review with no pasted draft falls back to the last thing generated.
    if "draft" not in brief and session["last_draft"]:
        brief["draft"] = session["last_draft"]

    # A revision needs the draft it is revising. Without this the Generator would
    # regenerate from the brief alone and "make it shorter" would have nothing to
    # be shorter than.
    if brief.get("revision_note") and session["last_draft"]:
        brief["previous_draft"] = session["last_draft"]
    else:
        brief.pop("previous_draft", None)
        brief.pop("revision_note", None)

    session["history"].append({"role": "user", "text": message})
    _touch_session(session, message)

    result = {
        "reply": "",
        "plan": intents,
        "reasoning": reasoning,
        "is_followup": is_followup,
        "ran": [],
    }

    unbuilt = [i for i in intents if i in NOT_BUILT]
    if unbuilt:
        result["reply"] = " ".join(NOT_BUILT[i] for i in unbuilt)
        _record_agent_turn(session_id, session, result)
        yield {"type": "result", **result}
        return

    runnable = [i for i in intents if i in AGENTS]
    if not runnable:
        result["reply"] = (
            "I can write a draft, review one, repurpose one into another format, or "
            "build a content calendar. Tell me what to write about and which channel, "
            "paste a draft you want scored, say what to turn existing content into, or "
            "ask for a plan for a given period."
        )
        _record_agent_turn(session_id, session, result)
        yield {"type": "result", **result}
        return

    # A generate request naming several channels in one message ("a LinkedIn
    # post and an Instagram caption about X") means N distinct pieces, not one
    # chain — handled as its own branch, ahead of the single-channel paths below.
    channels_field = brief.get("channels")
    if (
        isinstance(channels_field, list)
        and len(channels_field) > 1
        and "generate" in runnable
        and set(runnable) <= MULTI_CHANNEL_INTENTS
    ):
        yield from _run_multi_channel_stream(
            channels_field, "review" in runnable, auto_revise, brief, session, result, session_id
        )
        return

    # Auto-revise replaces the normal sequential run: instead of generating once
    # and reporting the score, it loops until the Reviewer passes the draft.
    if auto_revise and runnable[:2] == ["generate", "review"]:
        gen_spec = AGENTS["generate"]
        gen_params, missing = _resolve_params("generate", gen_spec, brief, None)

        if missing:
            asked = [QUESTIONS.get(m, f"I need a value for {m}.") for m in missing]
            result["reply"] = " ".join(asked)
            result["needs"] = missing
            _record_agent_turn(session_id, session, result)
            yield {"type": "result", **result}
            return

        # A revision note from the user's own message applies to the first draft
        # only; after that the Reviewer's feedback drives each pass.
        intent_brief = {k: brief[k] for k in BRIEF_INTENT_KEYS if brief.get(k)}
        draft, report, attempts = yield from _auto_revise_stream(
            gen_spec, AGENTS["review"], gen_params, gen_params["channel"], intent_brief
        )

        session["last_draft"] = draft
        brief["draft"] = draft
        result["draft"] = draft
        result["report"] = report
        result["ran"] = ["generate", "review"]
        result["attempts"] = attempts
        result["auto_revised"] = True
        result["channel"] = gen_params["channel"]

        passed = report.get("verdict") == "pass"
        n = len(attempts)
        tries = "1 attempt" if n == 1 else f"{n} attempts"
        if passed:
            result["reply"] = (
                f"Here's a {gen_params['channel']} draft — passed at "
                f"{report.get('overall_score')}/100 after {tries}."
            )
        else:
            result["reply"] = (
                f"Here's a {gen_params['channel']} draft. Still {report.get('overall_score')}/100 "
                f"after {tries}, so it needs a human look. {report.get('summary', '')}"
            )
        _record_agent_turn(session_id, session, result)
        yield {"type": "result", **result}
        return

    # Execute the plan, passing each step's output to the next.
    chain_output = None
    replies = []

    for intent in runnable:
        spec = AGENTS[intent]
        params, missing = _resolve_params(intent, spec, brief, chain_output)

        if missing:
            asked = [QUESTIONS.get(m, f"I need a value for {m}.") for m in missing]
            result["reply"] = " ".join(asked)
            result["needs"] = missing
            _record_agent_turn(session_id, session, result)
            yield {"type": "result", **result}
            return

        # Generator/Repurposer calls stream their text live; everything else
        # (Reviewer, Planner) runs as one blocking call, same as before.
        if spec.get("stream_fn"):
            channel_for_card = params.get("channel") or FORMAT_TO_CHANNEL.get(params.get("target_format"))
            output = yield from _stream_text_agent(spec["produces"], spec["stream_fn"], params, channel_for_card)
        else:
            output = spec["fn"](**params)

        result[spec["produces"]] = output
        result["ran"].append(intent)
        chain_output = output

        if spec["produces"] == "draft":
            session["last_draft"] = output
            brief["draft"] = output
            replies.append(f"Here's a {params['channel']} draft.")
        elif spec["produces"] == "calendar":
            # A plan is not content, so it does not become last_draft — a later
            # "make it shorter" must not try to revise a calendar.
            slots = output.get("slots", [])
            channels = sorted({s.get("channel") for s in slots if s.get("channel")})
            replies.append(
                f"Planned {len(slots)} slots for {output.get('timeframe', 'the period')}"
                f"{' across ' + ', '.join(channels) if channels else ''}. "
                f"{output.get('coverage_notes', '')}"
            )
        elif spec["produces"] == "repurposed":
            # The repurposed piece becomes the current draft, so a following
            # review scores it rather than the source it came from. Its channel
            # follows from the format, which is what the Reviewer needs.
            session["last_draft"] = output
            brief["draft"] = output
            channel = FORMAT_TO_CHANNEL.get(params["target_format"])
            if channel:
                brief["channel"] = channel
            chain_output = output
            replies.append(f"Repurposed into {params['target_format']}.")
        elif spec["produces"] == "report":
            verdict = "Looks good" if output.get("verdict") == "pass" else "Needs work"
            replies.append(f"{verdict} — {output.get('overall_score')}/100. {output.get('summary', '')}")

    # Lets the frontend offer a "Review" action on a draft/repurposed card
    # without asking the user which channel it was written for.
    if brief.get("channel"):
        result["channel"] = brief["channel"]

    result["reply"] = " ".join(replies)
    _record_agent_turn(session_id, session, result)
    yield {"type": "result", **result}


def handle_message_stream(message, session_id="default"):
    """Same turn as `handle_message`, exposed as the raw event generator so a
    caller (the Flask route) can forward text_start/text_chunk/text_done
    events to the client as they happen."""
    return _handle_message_events(message, session_id)


def handle_message(message, session_id="default"):
    """Full turn: route, execute the plan, assemble one response.

    Drains the streaming core and returns just the final result — for callers
    that don't care about live text, this behaves exactly as it always did.
    """
    result = None
    for event in _handle_message_events(message, session_id):
        if event.get("type") == "result":
            result = {k: v for k, v in event.items() if k != "type"}
    return result
