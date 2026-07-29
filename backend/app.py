from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from generator import generate_draft
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


@app.route("/api/review", methods=["POST"])
def review():
    data = request.get_json(silent=True) or {}

    draft = (data.get("draft") or "").strip()
    channel = (data.get("channel") or "").strip()

    if not draft or not channel:
        return jsonify({"error": "draft and channel are required"}), 400

    try:
        report = review_draft(draft, channel)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Review failed: {e}"}), 502

    return jsonify({"report": report})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
