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

from dotenv import load_dotenv
from google import genai
from google.genai import types

from generator import generate_draft
from planner import ICPS, PILLARS, generate_plan
from repurposer import repurpose_content
from reviewer import review_draft

load_dotenv()

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client = None

CHANNELS = ["LinkedIn", "X (Twitter)", "Instagram", "Email", "Blog"]


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
  ("twitter"/"tweet" -> "X (Twitter)", "IG"/"insta" -> "Instagram", "newsletter" -> "Email").
  If no channel is stated or implied, omit the field entirely.
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
- pillars, theme, icp, posts_per_week: plan requests only, and only if the user says so.
  theme is a campaign or thread to build the calendar around ("our diaspora push").
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
# Conversation memory only — the last few turns plus the running brief, so
# follow-ups resolve. This is deliberately in-process: it is correct for a
# single-process dev server and dies on restart. The vector-DB knowledge layer
# in section 5 of the brief is a separate concern (phase 4).

SESSIONS = {}
MAX_HISTORY = 6


def _get_session(session_id):
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {"history": [], "brief": {}, "last_draft": None}
    return SESSIONS[session_id]


def reset_session(session_id):
    SESSIONS.pop(session_id, None)


def _history_block(session):
    if not session["history"]:
        return ""
    turns = "\n".join(f"{t['role']}: {t['text']}" for t in session["history"][-MAX_HISTORY:])
    return f"CONVERSATION SO FAR:\n{turns}\n"


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


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
        ),
    )
    routed = json.loads(response.text)

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


def _auto_revise(generate_spec, review_spec, gen_params, channel, brief=None):
    """Generate, review, and revise until the draft passes or attempts run out.

    This is the one genuinely agentic loop in the system: the Reviewer's verdict
    drives another Generator call with no user input. It is bounded on purpose.

    The brief goes to the Reviewer as well as the Generator. Without it the loop
    can spend every attempt chasing a rule the brief exempted the piece from.
    """
    attempts = []
    draft = generate_spec["fn"](**gen_params)
    review_kwargs = {"brief": brief} if brief else {}

    for _ in range(MAX_REVISE_ATTEMPTS):
        report = review_spec["fn"](draft=draft, channel=channel, **review_kwargs)
        attempts.append({"score": report.get("overall_score"), "verdict": report.get("verdict")})

        if report.get("verdict") == "pass":
            return draft, report, attempts

        note = _revision_note_from(report)
        if not note:
            return draft, report, attempts

        draft = generate_spec["fn"](**{**gen_params, "previous_draft": draft, "revision_note": note})

    # Out of attempts: score the final revision so the caller reports the draft
    # it is actually returning, not the previous one's verdict.
    report = review_spec["fn"](draft=draft, channel=channel, **review_kwargs)
    attempts.append({"score": report.get("overall_score"), "verdict": report.get("verdict")})
    return draft, report, attempts


def handle_message(message, session_id="default"):
    """Full turn: route, execute the plan, assemble one response."""
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
        session["history"].append({"role": "agent", "text": result["reply"]})
        return result

    runnable = [i for i in intents if i in AGENTS]
    if not runnable:
        result["reply"] = (
            "I can write a draft, review one, repurpose one into another format, or "
            "build a content calendar. Tell me what to write about and which channel, "
            "paste a draft you want scored, say what to turn existing content into, or "
            "ask for a plan for a given period."
        )
        session["history"].append({"role": "agent", "text": result["reply"]})
        return result

    # Auto-revise replaces the normal sequential run: instead of generating once
    # and reporting the score, it loops until the Reviewer passes the draft.
    if auto_revise and runnable[:2] == ["generate", "review"]:
        gen_spec = AGENTS["generate"]
        gen_params, missing = _resolve_params("generate", gen_spec, brief, None)

        if missing:
            asked = [QUESTIONS.get(m, f"I need a value for {m}.") for m in missing]
            result["reply"] = " ".join(asked)
            result["needs"] = missing
            session["history"].append({"role": "agent", "text": result["reply"]})
            return result

        # A revision note from the user's own message applies to the first draft
        # only; after that the Reviewer's feedback drives each pass.
        intent_brief = {k: brief[k] for k in BRIEF_INTENT_KEYS if brief.get(k)}
        draft, report, attempts = _auto_revise(
            gen_spec, AGENTS["review"], gen_params, gen_params["channel"], intent_brief
        )

        session["last_draft"] = draft
        brief["draft"] = draft
        result["draft"] = draft
        result["report"] = report
        result["ran"] = ["generate", "review"]
        result["attempts"] = attempts
        result["auto_revised"] = True

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
        session["history"].append({"role": "agent", "text": result["reply"]})
        return result

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
            session["history"].append({"role": "agent", "text": result["reply"]})
            return result

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

    result["reply"] = " ".join(replies)
    session["history"].append({"role": "agent", "text": result["reply"]})
    return result
