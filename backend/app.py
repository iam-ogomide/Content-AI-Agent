import json
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context

from exporter import calendar_filename, calendar_to_xlsx_bytes
from generator import generate_draft
from planner import generate_plan
from repurposer import repurpose_content

from orchestrator import get_history, handle_message_stream, list_sessions, reset_session
from reviewer import review_draft

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}

    topic = (data.get("topic") or "").strip()
    channel = (data.get("channel") or "").strip()
    tone = (data.get("tone") or "").strip()
    audience = (data.get("audience") or "").strip() or None
    cta = (data.get("cta") or "").strip() or None
    keyword = (data.get("keyword") or "").strip() or None

    if not topic or not channel or not tone:
        return jsonify({"error": "topic, channel, and tone are required"}), 400

    try:
        draft = generate_draft(topic, channel, tone, audience, cta, keyword)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Generation failed: {e}"}), 502

    return jsonify({"draft": draft})


@app.route("/api/repurpose", methods=["POST"])
def repurpose():
    data = request.get_json(silent=True) or {}

    source_content = (data.get("source_content") or "").strip()
    target_format = (data.get("target_format") or "").strip()
    tone_shift = (data.get("tone_shift") or "").strip() or None
    word_limit = (data.get("word_limit") or "").strip() or None

    if not source_content or not target_format:
        return jsonify({"error": "source_content and target_format are required"}), 400

    try:
        result = repurpose_content(source_content, target_format, tone_shift, word_limit)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Repurposing failed: {e}"}), 502

    return jsonify({"result": result})


@app.route("/api/plan", methods=["POST"])
def plan():
    data = request.get_json(silent=True) or {}

    timeframe = (data.get("timeframe") or "").strip()
    channels = data.get("channels") or []
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.split(",") if c.strip()]

    pillars = data.get("pillars") or None
    theme = (data.get("theme") or "").strip() or None
    icp = (data.get("icp") or "").strip() or None
    posts_per_week = data.get("posts_per_week") or None

    if not timeframe or not channels:
        return jsonify({"error": "timeframe and channels are required"}), 400

    try:
        calendar = generate_plan(timeframe, channels, pillars, theme, icp, posts_per_week)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Planning failed: {e}"}), 502

    return jsonify({"calendar": calendar})


@app.route("/api/plan/export", methods=["POST"])
def export_plan():
    data = request.get_json(silent=True) or {}
    calendar = data.get("calendar")

    if not isinstance(calendar, dict) or not calendar.get("slots"):
        return jsonify({"error": "calendar with slots is required"}), 400

    try:
        buf = calendar_to_xlsx_bytes(calendar)
    except Exception as e:
        return jsonify({"error": f"Export failed: {e}"}), 500

    return send_file(
        buf,
        as_attachment=True,
        download_name=calendar_filename(calendar),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/review", methods=["POST"])
def review():
    data = request.get_json(silent=True) or {}

    draft = (data.get("draft") or "").strip()
    channel = (data.get("channel") or "").strip()

    # Optional. What the piece was asked to be — brand rules with stated
    # exceptions (section 3's one-person rule, section 8's CTA expectation)
    # can't resolve without it, so the Reviewer defaults to penalizing.
    # Accepts a dict of brief fields, or a plain sentence of intent.
    brief = data.get("brief") or None
    if isinstance(brief, str):
        brief = {"asked for": brief.strip()} if brief.strip() else None
    elif not isinstance(brief, dict):
        brief = None

    if not draft or not channel:
        return jsonify({"error": "draft and channel are required"}), 400

    try:
        report = review_draft(draft, channel, brief)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Review failed: {e}"}), 502

    return jsonify({"report": report})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}

    message = (data.get("message") or "").strip()
    session_id = (data.get("session_id") or "default").strip() or "default"

    if not message:
        return jsonify({"error": "message is required"}), 400

    # Newline-delimited JSON: one event object per line. text_start/text_chunk/
    # text_done stream a draft or repurposed piece as Gemini generates it; the
    # final "result" line carries the same payload /api/chat used to return
    # outright, for whatever didn't stream (reply text, review report, calendar).
    # Errors surface as their own event rather than an HTTP status, since the
    # response has already started by the time one can occur.
    def event_stream():
        try:
            for event in handle_message_stream(message, session_id):
                yield json.dumps(event) + "\n"
        except RuntimeError as e:
            yield json.dumps({"type": "error", "error": str(e)}) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "error": f"Orchestration failed: {e}"}) + "\n"

    return Response(stream_with_context(event_stream()), mimetype="application/x-ndjson")


@app.route("/api/chat/history", methods=["GET"])
def chat_history():
    """Past turns for a session, so a page reload can redraw the conversation.

    An unknown session id is not an error — it returns an empty list, which is
    what a genuinely new conversation looks like.
    """
    session_id = (request.args.get("session_id") or "default").strip() or "default"
    return jsonify({"history": get_history(session_id)})


@app.route("/api/chat/sessions", methods=["GET"])
def chat_sessions():
    """Sidebar listing: one metadata entry per stored conversation, newest first.

    Metadata only — the history for whichever one the user clicks is fetched
    separately, so opening the app doesn't ship every draft ever written.
    """
    return jsonify({"sessions": list_sessions()})


@app.route("/api/chat/reset", methods=["POST"])
def chat_reset():
    """Delete a conversation outright.

    Note this is a delete, not a "start a new one" — starting a new conversation
    needs no server call at all, since the frontend just mints a new session id.
    """
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "default").strip() or "default"
    reset_session(session_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
