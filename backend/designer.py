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
import json
import os
import re
import ssl
from pathlib import Path

import httpx2
from canva_auth import get_canva_auth
from dotenv import load_dotenv
from google import genai
from google.genai import _mcp_utils as genai_mcp_utils
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from tracing import model_span

load_dotenv()

# ---------------------------------------------------------------------------
# COMPAT SHIMS for google-genai's MCP support. Both are patches to library
# internals, so keep them together, keep them small, and delete them the
# moment upstream fixes the underlying bugs. Verified against google-genai
# 2.15.0 + mcp 2.0.0; without them, passing a live session to
# generate_content cannot even list Canva's tools.
# ---------------------------------------------------------------------------

# SHIM 1 — mcp 1.x/2.0 field rename. google-genai's MCP layer
# (_mcp_utils.mcp_to_gemini_tool, and _extra_utils' tool-response handling)
# reads mcp 1.x's camelCase field names; mcp 2.0 renamed them to snake_case,
# so the call dies with "AttributeError: 'Tool' object has no attribute
# 'inputSchema'". Checked as far as google-genai 2.17.0 — still camelCase
# there, so upgrading is not the fix. These read-only aliases let both
# libraries see the names they expect. If a future google-genai reaches for
# another camelCase field, this fails the same loud way — add it here.
for _cls, _camel, _snake in (
    (mcp_types.Tool, "inputSchema", "input_schema"),
    (mcp_types.CallToolResult, "isError", "is_error"),
):
    if not hasattr(_cls, _camel):
        setattr(_cls, _camel, property(lambda self, _n=_snake: getattr(self, _n)))

# SHIM 2 — google-genai bug, unrelated to the mcp version. Its schema
# filter recurses into "items"/"additionalProperties" assuming a dict, but
# `additionalProperties: false` is ordinary JSON Schema, so it blows up with
# "'bool' object has no attribute 'items'". 33 of 33 Canva tool schemas hit
# this. Passing non-dict values straight through fixes all of them (verified:
# all 33 convert). The recursive calls inside the original resolve this
# module global by name, so nested occurrences are covered too.
_orig_filter_schema = genai_mcp_utils._filter_to_supported_schema


def _filter_schema_allowing_non_dicts(schema):
    if not isinstance(schema, dict):
        return schema
    return _orig_filter_schema(schema)


genai_mcp_utils._filter_to_supported_schema = _filter_schema_allowing_non_dicts

BRAND_VOICE_PATH = Path(__file__).resolve().parent.parent / "brand_voice.md"
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CANVA_MCP_URL = os.getenv("CANVA_MCP_URL", "https://mcp.canva.com/mcp")
CANVA_READ_TIMEOUT = float(os.getenv("CANVA_READ_TIMEOUT", "300"))

_client = None

# Per-channel graphic spec. Kept here rather than in generator.py because these
# describe the graphic, not the copy — a LinkedIn post and its graphic have
# different size constraints entirely.
#
# design_type MUST be a member of generate-design's enum. Canva offers 26 types
# and none of them is LinkedIn, so asking for "1200x627" in prose (as this used
# to) just made the model guess — it picked facebook_cover, which is ~820x312,
# and produced a design literally titled "Facebook Cover". So name the type here.
#
# Pick the type whose NATIVE canvas is closest in aspect ratio to the target,
# because _fit_design_to_spec has to Magic Resize the gap and a big resize
# re-flows the layout badly — text overruns its box and long lines get clipped.
# Measured natively: twitter_post 1600x900 (1.78), facebook_post 940x788 (1.19),
# youtube_thumbnail 1280x720 (1.78), instagram_post 1080x1080 (1.00).
# So every landscape channel is built on twitter_post: for LinkedIn's 1.90
# target that is a 7% adjustment, where facebook_post was 37% and visibly
# mangled the result.
#
# width/height are the target export dimensions. design_type only sets the
# canvas the design is composed on, and its aspect ratio does NOT have to match:
# _fit_design_to_spec below reads the real canvas size back from Canva and Magic
# Resizes the design when the aspect ratio differs, so nothing is ever stretched
# to hit these numbers.
CHANNEL_SPECS = {
    "Instagram": {"design_type": "instagram_post", "width": 1080, "height": 1080,
                  "label": "1080x1080 square post"},
    "LinkedIn": {"design_type": "twitter_post", "width": 1200, "height": 630,
                 "label": "1200x630 landscape post"},
    # Native match, so no resize happens at all for this one.
    "X (Twitter)": {"design_type": "twitter_post", "width": 1600, "height": 900,
                    "label": "1600x900 landscape post"},
    # NOT design_type "email": that builds a full newsletter layout, a tall
    # multi-section page, and squeezing one into a 2:1 banner destroys it. What's
    # wanted here is a wide header graphic, so start from a wide canvas.
    "Email": {"design_type": "twitter_post", "width": 1200, "height": 600,
              "label": "landscape email header graphic"},
    "Blog": {"design_type": "twitter_post", "width": 1200, "height": 630,
             "label": "1200x630 landscape header image"},
}

# Fallback for a design request whose channel isn't a publishing surface. The
# router's CHANNELS list includes "Graphic", which means graphic *copy* for a
# human designer, not a place to publish — so it has no dimensions of its own
# and lands here. A square is the most reusable shape when the destination is
# genuinely unknown. The router is also told not to route design requests to
# "Graphic" in the first place (see orchestrator's EXTRACTION RULES); this is
# the backstop for when it does anyway.
DEFAULT_SPEC = {"design_type": "instagram_post", "width": 1080, "height": 1080,
                "label": "1080x1080 square post"}

# Tools whose result carries a design_summary we can pull a design_id out of.
# generate-design alone is not enough: it returns candidates, and a candidate
# only becomes a real, exportable design once one of these has run.
_DESIGN_CREATING_TOOLS = {
    "create-design-from-candidate",
    "create-design-from-brand-template",
    "copy-design",
    "import-design-from-url",
}


def _unwrap(error):
    """The innermost cause inside anyio's nested ExceptionGroups."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


# Connection failures that are worth another try rather than a report.
_TRANSIENT = (ssl.SSLError, httpx2.TransportError, ConnectionError, TimeoutError)

# One extra attempt, not a long chain: if the connection is genuinely down,
# five tries just make the user wait five times as long for the same answer.
_FLOW_ATTEMPTS = 2


def _run_canva(make_coro):
    """Run one Canva flow, surfacing the real error instead of anyio's wrapper.

    Takes a callable that builds the coroutine, because a coroutine can only be
    awaited once and this may need a second attempt.

    Two problems are handled here, both of which reached the user as the same
    useless string, "unhandled errors in a TaskGroup (1 sub-exception)":

    1. anyio re-raises anything thrown inside the MCP session's task group as an
       ExceptionGroup, so the real cause never appeared in the message. Unwrap.
    2. The transport does its POSTs in a task INSIDE that group, so a dropped
       connection never passes through _call's await and cannot be retried per
       tool — it tears the whole session down instead. Retry the flow.

    Retrying is only safe before the flow has changed anything: replaying a flow
    that already created a design or committed an edit would do it twice. The
    flow itself reports that via mutation_state, and a flow that has mutated
    raises instead of retrying. Most of these blips land on the very first
    request of the session, which is exactly the case this can rescue.
    """
    for attempt in range(1, _FLOW_ATTEMPTS + 1):
        state = {"mutated": False}
        try:
            return asyncio.run(make_coro(state))
        except BaseException as error:  # noqa: BLE001 — re-raised below
            cause = _unwrap(error) if isinstance(error, BaseExceptionGroup) else error
            retryable = (
                isinstance(cause, _TRANSIENT)
                and not state["mutated"]
                and attempt < _FLOW_ATTEMPTS
            )
            if not retryable:
                if cause is error:
                    raise
                raise cause from error
            print(
                f"Canva connection dropped ({type(cause).__name__}) before anything "
                f"was created; retrying once.",
                flush=True,
            )


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
    spec = CHANNEL_SPECS.get(channel, DEFAULT_SPEC)

    brief_lines = [
        f"Topic: {topic}",
        f"Channel: {channel}",
        f"Format: {spec['label']}",
        f"Canva design_type to use: {spec['design_type']}",
    ]
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
Use the Canva tools available to you to create ONE on-brand visual for the brief below.

--- BRAND VOICE DOC (use for tone, color, and typography cues where it gives them) ---
{brand_voice}
--- END BRAND VOICE DOC ---

DESIGN BRIEF:
{brief}

Instructions:
- Create a single design using exactly the design_type named in the brief. Do not
  substitute a different type, and do not pick one based on the pixel dimensions.
- Generate design candidates, then turn your chosen candidate into a real design so it
  has a design ID. A candidate on its own is not a design.
- Pass length "short" to generate-design. This is not a stylistic preference: "balanced"
  fills the design with a full paragraph, and the graphic is later resized to the
  channel's exact shape, at which point a paragraph overruns its text box and the last
  line gets clipped off. Short copy survives that step; long copy does not.
- Keep on-design text minimal: a headline of about 6 words or fewer, and at most one
  supporting line of about 12 words. This is a graphic to accompany a post, not the post
  itself — do not try to fit the full copy on it.
- Prefer a candidate with no leftover template placeholder text (a fake website like
  "reallygreatsite.com", a dummy address, "Your Company Name", lorem ipsum).
- Follow the brand voice doc's tone and any stated visual cues (colors, typography).
  Do not invent brand colors or fonts the doc does not mention — if it is silent on
  visual style, keep the design clean and minimal rather than guessing at a look.
- Do NOT export the design and do NOT call any export tool — exporting is handled in
  code after you finish, at the exact size this channel needs.
- Finish by stating the design ID and a one-line description of what you made.
"""


def _tool_result_payloads(function_response):
    """Pull the decoded JSON payloads out of one MCP tool response.

    google-genai wraps the CallToolResult as {"result": <CallToolResult>}, but
    depending on the code path it can arrive as the pydantic object or as a
    plain dict, and each content block is text that happens to hold JSON.
    Tolerate all of that rather than assuming one shape.
    """
    result = (function_response or {}).get("result")
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")

    payloads = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if not text:
            continue
        try:
            payloads.append(json.loads(text))
        except (ValueError, TypeError):
            continue  # not every content block is JSON; skip the ones that aren't
    return payloads


def _find_created_design(response):
    """Find the design the model actually created, from its tool-call history.

    Reads the design ID out of the tool results rather than out of the model's
    prose. The model's closing sentence is free text and has already been
    observed to arrive truncated or missing entirely — the tool history is the
    part that reflects what really happened in Canva.

    Returns a dict with id/edit_url/view_url/title, or None if no design was
    created. Takes the last match, so a retry supersedes an earlier attempt.
    """
    found = None
    for content in response.automatic_function_calling_history or []:
        for part in content.parts or []:
            call = getattr(part, "function_response", None)
            if not call or call.name not in _DESIGN_CREATING_TOOLS:
                continue
            for payload in _tool_result_payloads(call.response):
                summary = payload.get("design_summary") or {}
                if summary.get("id"):
                    urls = summary.get("urls") or {}
                    found = {
                        "id": summary["id"],
                        "title": summary.get("title"),
                        "edit_url": urls.get("edit_url"),
                        "view_url": urls.get("view_url"),
                    }
    return found


# Canva's connection drops occasionally mid-call — observed as
# "ssl.SSLError: SSLV3_ALERT_BAD_RECORD_MAC" and as read timeouts, several times
# over an afternoon of testing. Retrying is only safe for calls that don't change
# anything, or that can be repeated without a second side effect, so this is an
# allowlist rather than a blanket retry:
#   - get-design-pages just measures.
#   - export-design re-renders the same design to a new URL; a wasted render is
#     the only cost.
#   - start/cancel-editing-transaction: a retried start opens a fresh
#     transaction and the abandoned one expires on Canva's side.
# Deliberately NOT retried: resize-design (would leave a duplicate design),
# perform-editing-operations and commit (would apply an edit twice), and
# create-design-from-candidate (would create two designs).
_RETRYABLE_TOOLS = {
    "get-design-pages",
    "export-design",
    "start-editing-transaction",
    "cancel-editing-transaction",
}
_RETRY_ATTEMPTS = 3


async def _call(session, tool_name, args, what):
    """Call one Canva tool and return its decoded JSON payloads, or raise.

    Retries transient connection failures for the tools where that's safe (see
    _RETRYABLE_TOOLS). A tool that returns is_error is NOT retried: Canva
    answered, and the answer was no.
    """
    attempts = _RETRY_ATTEMPTS if tool_name in _RETRYABLE_TOOLS else 1
    for attempt in range(1, attempts + 1):
        try:
            result = await session.call_tool(tool_name, args)
            break
        except (ssl.SSLError, OSError, httpx2.TransportError) as e:
            if attempt == attempts:
                raise RuntimeError(
                    f"Lost the connection to Canva while {what}, {attempts} times "
                    f"running ({type(e).__name__}: {e}). Worth trying again."
                ) from e
            # Straight retry, no backoff: these are single dropped connections,
            # not rate limiting, and the calls are already slow enough.
            continue

    if result.is_error:
        raise RuntimeError(f"Canva {tool_name} failed while {what}: {result.content}")
    return _tool_result_payloads({"result": result})


async def _page_dimensions(session, design_id):
    """The design's real canvas size, as (width, height), or None if unreadable.

    get-design-pages is the cheap way to ask: start-editing-transaction also
    reports it but opens a transaction that then has to be committed or
    cancelled, which is a lot of ceremony for a measurement.
    """
    for payload in await _call(session, "get-design-pages",
                              {"design_id": design_id,
                               "user_intent": "Check the design's canvas dimensions "
                                             "before exporting."},
                              f"measuring design {design_id}"):
        for item in payload.get("items") or []:
            dims = item.get("dimensions") or {}
            if dims.get("width") and dims.get("height"):
                return dims["width"], dims["height"]
    return None


async def _fit_design_to_spec(session, design, spec):
    """Return a design whose canvas aspect ratio matches the channel's, resizing if needed.

    THIS EXISTS BECAUSE OF A REAL BUG, do not simplify it away. Exporting with
    an explicit width/height that disagrees with the canvas aspect ratio does
    not letterbox or crop — Canva stretches. A facebook_post canvas (940x788)
    exported at LinkedIn's 1200x630 came out squeezed 1.28x wide and 0.8x tall:
    circular logo marks rendered as ovals and the type looked compressed. That
    was the "bad image quality" the design itself was never responsible for.

    Magic Resize (resize-design) genuinely re-lays-out the design at the new
    ratio instead of scaling it, but it creates a NEW design, so the returned
    dict may point at a different design_id than the one passed in.
    """
    dims = await _page_dimensions(session, design["id"])
    if not dims:
        # Measurement failed, so we can't know whether a resize is needed.
        # Export native (see _export_design) rather than risk stretching.
        return design, None

    width, height = dims
    target_ratio = spec["width"] / spec["height"]
    # 1% covers types that are the right shape but a different scale (those
    # export to exact pixels with no distortion, so a resize would be waste).
    if abs((width / height) - target_ratio) / target_ratio <= 0.01:
        return design, (width, height)

    for payload in await _call(
        session,
        "resize-design",
        {
            "design_id": design["id"],
            "design_type": {"type": "custom", "width": spec["width"], "height": spec["height"]},
            "user_intent": f"Re-lay out the visual for {spec['label']} so it is not "
                           "distorted when exported.",
        },
        f"resizing design {design['id']} from {width}x{height} to "
        f"{spec['width']}x{spec['height']}",
    ):
        # Note the nesting: the new design is at job.result.design, NOT at the
        # top-level design_summary the create-design tools return.
        resized = ((payload.get("job") or {}).get("result") or {}).get("design") or {}
        if resized.get("id"):
            urls = resized.get("urls") or {}
            return {
                "id": resized["id"],
                "title": resized.get("title") or design.get("title"),
                "edit_url": urls.get("edit_url"),
                "view_url": urls.get("view_url"),
            }, (spec["width"], spec["height"])

    raise RuntimeError(
        f"Canva resize-design returned no new design for {design['id']} "
        f"({width}x{height} -> {spec['width']}x{spec['height']})"
    )


async def _export_design(session, design_id):
    """Export a finished design to PNG at its own canvas size.

    Native size on purpose: _fit_design_to_spec has already made the canvas the
    right shape, and passing width/height here is what distorted the output
    before. Any scaling to exact pixel dimensions must happen through a resize,
    never through the export.

    Driven from code, not by the model: export-design's `format` argument is a
    conditional object (type, then quality/size/width/height/lossless/... each
    only valid for certain types) and gemini-2.5-flash reliably produced a
    MALFORMED_FUNCTION_CALL on it, burning a whole design-generation run each
    time. Hard-coding the format here makes the step deterministic.
    """
    payloads = await _call(
        session,
        "export-design",
        {
            "design_id": design_id,
            "format": {"type": "png"},
            "user_intent": "Export the finished marketing visual as a PNG for publishing.",
        },
        f"exporting design {design_id}",
    )

    for payload in payloads:
        job = payload.get("job") or {}
        urls = job.get("urls") or []
        if job.get("status") == "success" and urls:
            return urls[0]
        if job.get("status") and job.get("status") != "success":
            raise RuntimeError(
                f"Canva export job for design {design_id} ended as "
                f"'{job['status']}' rather than success: {payload}"
            )
    raise RuntimeError(f"Canva export-design returned no URL for design {design_id}")


# Schema for the text-mapping step in a revision. The model's only job there is
# to decide which existing text element becomes what new text — it does not call
# any Canva tool, because the editing transaction is driven from code below.
_REVISION_SCHEMA = {
    "type": "object",
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "element_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["element_id", "text"],
            },
        },
        "no_change_reason": {"type": "string"},
    },
    "required": ["replacements"],
}

_REVISION_PROMPT = """You are the Designer Agent for CreditChek, editing the text on an
existing marketing visual.

--- BRAND VOICE DOC ---
{brand_voice}
--- END BRAND VOICE DOC ---

THE TEXT CURRENTLY ON THE DESIGN, in visual order (topmost first). `text height` is the
height of the text box in pixels, which tracks how big the type is on the design:
{elements}

Identifying which element is which, since the design does not label them: the element
with the LARGEST text height is the headline. A short line sitting directly above the
headline with much smaller text is a kicker/eyebrow label, NOT the headline — do not put
the new headline there. Lower elements with small text are supporting lines or CTAs.

THE USER'S REQUESTED CHANGE:
{instruction}

Return the replacements needed to satisfy that change, as element_id + the full new text
for that element.

Rules:
- Only include elements whose text actually changes. Leave everything else out — an
  element you omit keeps its current text, which is what you want.
- element_id must be copied exactly from the list above. Never invent one.
- `text` is the COMPLETE replacement for that element, not a fragment or a diff.
- These are text boxes on a graphic, sized for what they hold. Keep a replacement close
  in length to what it replaces: a headline that doubles in length will overflow its box
  or shrink to unreadable. Headlines stay short and punchy.
- Match the brand voice, and keep the text's role — a heading stays a heading, a
  supporting line stays a supporting line.
- If the change is impossible through text edits alone (it asks for different colors,
  images, or layout), return an empty replacements list and explain why in
  no_change_reason.
"""


def _element_text(item):
    """The visible text of one richtext element, joined across its regions.

    Canva splits a text box into regions (one per formatting run), so a single
    headline arrives as several pieces and there is no whole-box `text` field.
    """
    return " ".join(
        (region.get("text") or "") for region in (item.get("regions") or [])
    ).strip()


def _describe_elements(richtexts):
    """The design's text elements as a numbered list for the prompt.

    Includes each text box's height, because the text alone is not enough to
    tell a headline from the small kicker label above it — without this the
    model put a new headline into the eyebrow line and left the actual headline
    untouched. Height is used as the type-size signal since Canva reports the
    box, not the font size.
    """
    heights = [
        ((item.get("containerElement") or {}).get("dimension") or {}).get("height")
        for item in richtexts
    ]
    biggest = max((h for h in heights if h), default=None)

    lines = []
    for i, (item, height) in enumerate(zip(richtexts, heights), start=1):
        text = _element_text(item)
        detail = f"text height {round(height)}px" if height else "text height unknown"
        if height and height == biggest:
            detail += " — LARGEST text on the design"
        lines.append(
            f"{i}. element_id: {item.get('element_id')}\n"
            f"   current text: {text!r}\n"
            f"   {detail}"
        )
    return "\n".join(lines)


def _plan_replacements(richtexts, instruction):
    """Ask Gemini which text elements to change, as {element_id: new_text}.

    Split out from the Canva calls on purpose: gemini-2.5-flash handling Canva's
    editing tools directly means it also has to get transaction bookkeeping
    right, and a malformed call there leaves a transaction open on the design.
    Here the model only produces JSON and the code owns the transaction.
    """
    known = {item.get("element_id") for item in richtexts if item.get("element_id")}
    if not known:
        raise RuntimeError("This design has no editable text elements to change.")

    prompt = _REVISION_PROMPT.format(
        brand_voice=_load_brand_voice(),
        elements=_describe_elements(richtexts),
        instruction=instruction,
    )
    with model_span("plan_revision", prompt, MODEL_NAME) as span:
        response = _get_client().models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": _REVISION_SCHEMA,
                # Editing to instruction is a precision task, not a creative one.
                "temperature": 0.2,
            },
        )
        span.record(response)
    plan = json.loads(response.text)

    # Drop anything not actually on this design. A hallucinated element_id would
    # otherwise fail the whole perform-editing-operations call, losing the valid
    # replacements alongside it.
    replacements = [
        r for r in plan.get("replacements") or []
        if r.get("element_id") in known and r.get("text")
    ]
    if not replacements:
        # Say what IS on the design as well as why nothing changed. The model's
        # own reason is written about its input ("no supporting line was
        # included"), which reads like a bug to someone who just asked for a
        # shorter supporting line; the actual text is what lets them retarget
        # the request at something that exists.
        reason = (plan.get("no_change_reason") or "").strip()
        present = [t for t in (_element_text(item) for item in richtexts) if t]
        message = (
            f"I couldn't apply that change: {reason}" if reason
            else "I couldn't work out which text on the design that change applies to."
        )
        if present:
            listed = "; ".join(f'"{t[:60]}"' for t in present[:5])
            message += f" The text on this visual is: {listed}."
        raise RuntimeError(message)
    return replacements


# Canva's templates ship with dummy contact details, and its generator leaves
# them in — "reallygreatsite.com" showed up on a finished LinkedIn graphic.
# Prompting the model to avoid such candidates did NOT work (tried, still got
# one), so they're rewritten in code after the design exists. Substitutions
# only, never deletions: an empty text box is a hole in the layout, and
# creditchek.africa is the URL the brand voice doc's own boilerplate uses.
#
# Matching is deliberately narrow — these are Canva's stock placeholders, not a
# general profanity-style filter. Anything not listed is left alone, because
# silently rewriting real copy would be much worse than a stray placeholder.
BRAND_SITE = "creditchek.africa"

_PLACEHOLDER_SUBSTITUTIONS = (
    # Emails first: the domain rule below would otherwise turn
    # "hello@reallygreatsite.com" into "hello@creditchek.africa" via a partial
    # match, which is right, but being explicit keeps the intent readable.
    (re.compile(r"\b[\w.+-]+@(?:reallygreatsite|yourwebsite|yoursite|yourcompany|examplesite)\.[a-z]{2,}\b",
                re.I), f"hello@{BRAND_SITE}"),
    (re.compile(r"\b(?:www\.)?(?:reallygreatsite|yourwebsite|yoursite|yourcompany|examplesite)\.[a-z]{2,}\b",
                re.I), BRAND_SITE),
    (re.compile(r"\byour company name\b", re.I), "CreditChek"),
    (re.compile(r"\bcompany name\b", re.I), "CreditChek"),
)


def _plan_placeholder_fixes(richtexts):
    """Replacements that swap Canva's stock placeholder text for real details."""
    fixes = []
    for item in richtexts:
        element_id = item.get("element_id")
        original = " ".join(
            (region.get("text") or "") for region in (item.get("regions") or [])
        )
        if not element_id or not original.strip():
            continue

        text = original
        for pattern, replacement in _PLACEHOLDER_SUBSTITUTIONS:
            text = pattern.sub(replacement, text)
        if text != original:
            fixes.append({"element_id": element_id, "text": text})
    return fixes


async def _clean_placeholders(session, design_id):
    """Best-effort placeholder sweep. Returns what it changed, for logging.

    Never allowed to fail the run: a visual with a stray "reallygreatsite.com"
    is still a usable visual, whereas losing a design that took two minutes and
    a Gemini call to make over a failed cleanup is not a trade worth making.
    """
    try:
        _, _, fixes = await _apply_text_edits(
            session, design_id, _plan_placeholder_fixes,
            "replacing leftover template placeholder text",
        )
        return fixes
    except Exception:
        return []


async def _apply_text_edits(session, design_id, plan, what, mutation_state=None):
    """Edit the text on an existing design in place. Returns (dims, edit_url, replacements).

    `plan` is a function taking the design's text elements and returning the
    replacements to make, as [{element_id, text}]. It's a parameter because the
    two callers decide what to change very differently — one asks Gemini to
    interpret the user's instruction, the other pattern-matches placeholder text
    — while the transaction handling below is identical and worth having once.
    An empty plan commits nothing and cancels cleanly.

    mutation_state, if given, gets marked the moment this starts changing the
    design, so _run_canva knows the flow is no longer safe to replay.

    Canva edits are transactional: start, operate, commit. The try/finally is
    load-bearing — an uncommitted transaction stays open on the design and
    blocks the next edit, so a failure anywhere in the middle must cancel it.
    """
    payloads = await _call(session, "start-editing-transaction",
                          {"design_id": design_id,
                           "user_intent": f"Edit the visual's text: {what}"},
                          f"opening an edit on design {design_id}")

    transaction_id = None
    richtexts, dims, edit_url = [], None, None
    for payload in payloads:
        transaction_id = (payload.get("transaction") or {}).get("transaction_id") or transaction_id
        richtexts = payload.get("richtexts") or richtexts
        edit_url = payload.get("edit_design_url") or edit_url
        for page in payload.get("pages") or []:
            d = page.get("dimension") or {}
            if d.get("width") and d.get("height"):
                dims = (d["width"], d["height"])
                break
    if not transaction_id:
        raise RuntimeError(f"Canva did not return a transaction for design {design_id}")

    committed = False
    try:
        replacements = plan(richtexts)
        if not replacements:
            # Nothing to do. The finally block cancels, which is the right
            # outcome: committing an empty transaction would still count as an
            # edit on the design.
            return dims, edit_url, []

        # page_index is required per call, so operations are grouped by the page
        # their element sits on. In practice a marketing visual is one page.
        page_of = {
            item["element_id"]: item.get("page_index", 1)
            for item in richtexts if item.get("element_id")
        }
        by_page = {}
        for r in replacements:
            by_page.setdefault(page_of.get(r["element_id"], 1), []).append(
                {"type": "replace_text", "element_id": r["element_id"], "text": r["text"]}
            )

        if mutation_state is not None:
            mutation_state["mutated"] = True

        for page_index, operations in by_page.items():
            await _call(session, "perform-editing-operations",
                        {"transaction_id": transaction_id, "page_index": page_index,
                         "operations": operations,
                         "user_intent": f"Apply the requested text change: {what}"},
                        f"editing page {page_index} of design {design_id}")

        await _call(session, "commit-editing-transaction",
                    {"transaction_id": transaction_id,
                     "user_intent": "Save the edited visual."},
                    f"saving edits to design {design_id}")
        committed = True
        return dims, edit_url, replacements
    finally:
        if not committed:
            try:
                await _call(session, "cancel-editing-transaction",
                            {"transaction_id": transaction_id,
                             "user_intent": "Discard an edit that could not be completed."},
                            f"discarding a failed edit on design {design_id}")
            except Exception:
                # The original failure is the useful one; don't mask it with a
                # cleanup error. Worst case the transaction expires on Canva's side.
                pass


async def _try_export(session, design_id, done="The design is saved in Canva"):
    """Export the design, or report why not. Returns (export_url, export_error).

    Only for use AFTER the design has been changed. The export is the last step,
    and by the time it runs the real work is committed in Canva — so a dropped
    connection here must not raise, or the caller reports failure for a change
    that actually went through and the user is told nothing about a design that
    now exists. No preview, plus a link, beats a false failure.
    """
    try:
        return await _export_design(session, design_id), None
    except Exception as e:
        return None, (
            f"{done}, but the preview image couldn't be exported "
            f"({type(e).__name__}). Open the design in Canva to see it."
        )


async def _revise_visual_async(design_id, instruction, channel, mutation_state):
    http_client = httpx2.AsyncClient(
        auth=get_canva_auth(),
        timeout=httpx2.Timeout(CANVA_READ_TIMEOUT, connect=15.0),
    )
    async with streamable_http_client(CANVA_MCP_URL, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            dims, edit_url, replacements = await _apply_text_edits(
                session, design_id,
                lambda richtexts: _plan_replacements(richtexts, instruction),
                instruction,
                mutation_state,
            )
            if not replacements:
                # _plan_replacements raises rather than returning empty, so this
                # is unreachable today; it stays as the guard for anyone who
                # makes the planner lenient later.
                raise RuntimeError("Nothing on the design matched that change.")
            # No resize here: editing text doesn't change the canvas, and this
            # design has already been fitted to the channel by the run that
            # created it.
            export_url, export_error = await _try_export(
                session, design_id, "The change was saved in Canva"
            )

    return {
        "export_url": export_url,
        "export_error": export_error,
        "design_id": design_id,
        # Canva's own edit link, from the transaction — not constructed here, so
        # it stays correct if their URL shape changes.
        "edit_url": edit_url,
        "channel": channel,
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "revised": [r["text"] for r in replacements],
    }


def revise_visual(design_id, instruction, channel=None):
    """Change the text on an existing visual and re-export it.

    Edits the design in place rather than generating a new one, so everything
    the user already approved about the visual — layout, imagery, colors —
    survives a wording change. Only text can be changed this way; a request for
    different imagery or colors raises RuntimeError explaining that.

    Returns the same shape as generate_visual (minus title/notes, plus
    `revised`), so callers and the frontend render it identically. export_url
    expires the same way — see generate_visual's docstring.
    """
    if not design_id or not instruction:
        raise ValueError("design_id and instruction are required")

    return _run_canva(
        lambda state: _revise_visual_async(design_id, instruction, channel, state)
    )


async def _generate_visual_async(topic, channel, headline, draft_excerpt, style_note,
                                 mutation_state):
    prompt = build_prompt(topic, channel, headline, draft_excerpt, style_note)
    spec = CHANNEL_SPECS.get(channel, DEFAULT_SPEC)
    client = _get_client()

    # NOTE: this version of the mcp package (2.0.0) vendors its own httpx fork
    # (httpx2) and no longer takes auth= directly on streamable_http_client —
    # the OAuth provider has to be attached to an httpx2.AsyncClient instead.
    # If you upgrade mcp later and this breaks again, check
    # inspect.signature(streamable_http_client) first rather than guessing.
    # Long read timeout, not httpx's 5s default: Canva's generate-design and
    # export-design calls do real rendering work server-side and routinely take
    # far longer than that. Short timeouts here surface as a bare
    # httpx2.ReadTimeout mid-run, after a design may already have been created.
    http_client = httpx2.AsyncClient(
        auth=get_canva_auth(),
        timeout=httpx2.Timeout(CANVA_READ_TIMEOUT, connect=15.0),
    )

    async with streamable_http_client(CANVA_MCP_URL, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # config MUST stay a plain dict, not a GenerateContentConfig. google-genai
            # 2.15 calls config.model_copy(deep=True) on a config object, which
            # deep-copies the live MCP session and dies with "cannot pickle
            # '_asyncio.Task'". The dict branch rebuilds the config instead of
            # copying it, so the session passes through untouched.
            #
            # Mutating from here on: the model creates designs through the
            # session, and once it may have created one this flow cannot be
            # replayed after a dropped connection without risking a duplicate.
            mutation_state["mutated"] = True
            # One span for the whole agentic turn. The Canva tool calls the model
            # makes inside it are not traced — see tracing.py — so this span's
            # duration includes Canva's work without breaking it down.
            with model_span("design_visual", prompt, MODEL_NAME) as span:
                response = await client.aio.models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt,
                    config={"tools": [session]},
                )
                span.record(response)

            design = _find_created_design(response)
            if not design:
                # Deliberately loud. An earlier version returned response.text
                # here, which meant a run that finished with
                # MALFORMED_FUNCTION_CALL and no design at all still came back
                # as a cheerful "Here's a preview of the generated design:" and
                # exited 0. A failure that reads as a success is worse than a
                # crash.
                finish_reason = None
                if response.candidates:
                    finish_reason = response.candidates[0].finish_reason
                raise RuntimeError(
                    "Designer Agent finished without creating a Canva design "
                    f"(finish_reason={finish_reason}). Model's last words: "
                    f"{(response.text or '')[:300]!r}"
                )

            # May hand back a different design than the model made — see
            # _fit_design_to_spec. Everything below must use the returned one.
            design, dims = await _fit_design_to_spec(session, design, spec)
            # After the resize, so the placeholders are fixed on the design that
            # actually gets exported, and before the export so the fix is in the
            # PNG rather than only in Canva.
            cleaned = await _clean_placeholders(session, design["id"])
            export_url, export_error = await _try_export(session, design["id"])

    return {
        "export_url": export_url,
        # None unless the design was made but the PNG couldn't be fetched. The
        # design still exists in Canva, so hand it over with its links rather
        # than throwing away a finished visual over a failed download.
        "export_error": export_error,
        "design_id": design["id"],
        "edit_url": design.get("edit_url"),
        "view_url": design.get("view_url"),
        # Our own label, not Canva's. Canva names the design after the base
        # design_type, so a LinkedIn visual came back titled "Twitter / X Post -
        # Verify Income in Seconds" — accurate about the canvas it was built on,
        # actively misleading about where the graphic is going. The design keeps
        # Canva's name inside Canva; this is only what the card displays.
        "title": f"{channel} — {headline or topic}",
        "channel": channel,
        # The exported PNG's real size, which the frontend uses to reserve the
        # right aspect ratio. Falls back to the spec's target only when the
        # canvas couldn't be measured.
        "width": dims[0] if dims else spec["width"],
        "height": dims[1] if dims else spec["height"],
        # What the placeholder sweep rewrote, if anything — worth surfacing so a
        # placeholder pattern that stops matching shows up as an empty list here
        # rather than silently reappearing on the graphic.
        "placeholders_fixed": cleaned,
        # response.text can be None or truncated — it's a nice-to-have note, not
        # the payload. Everything above is read from Canva's own tool results.
        "notes": (response.text or "").strip() or None,
    }


def generate_visual(topic, channel, headline=None, draft_excerpt=None, style_note=None):
    """Sync wrapper so this agent's call shape matches generate_draft / repurpose_content.

    Returns a dict: export_url, design_id, edit_url, view_url, title, channel,
    width, height, notes.

    export_url is a presigned Canva download link that EXPIRES — observed
    ~18 hours. Fine to hand to a browser right away; do not persist it as a
    lasting record of the asset. If visuals ever need to outlive that window,
    download the bytes and store them yourself, and keep design_id/edit_url as
    the durable references (those don't expire).

    Raises RuntimeError if no design was created, rather than returning the
    model's chatter — see _generate_visual_async for why.
    """
    if not topic or not channel:
        raise ValueError("topic and channel are required")

    return _run_canva(
        lambda state: _generate_visual_async(
            topic, channel, headline, draft_excerpt, style_note, state
        )
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
    print(json.dumps(result, indent=2))