"""LangSmith tracing for this system's Gemini calls. Optional, and off unless configured.

WHAT THIS TRACES: one span per Gemini call — the prompt sent, the text that came
back, token counts, latency, and the model name. That is deliberately the whole
scope. Canva's steps (generate, resize, placeholder sweep, export) are NOT
traced, so a slow export or a failed placeholder sweep will not appear here;
the terminal traceback from app.py is still the place to look for those.

TURNING IT ON — add to .env:
    LANGSMITH_TRACING=true
    LANGSMITH_API_KEY=<your key from smith.langchain.com>
    LANGSMITH_PROJECT=creditchek-content   # optional, groups the traces

WITHOUT those, every span in here is skipped before LangSmith is touched at all.
That check is a hard requirement, not tidiness: langsmith builds its run tree
even when tracing is disabled (measured ~1.5ms per span), and a prompt is
private data, so the default has to be to do nothing.

Prompts and completions ARE sent to LangSmith's servers when this is on. For
this system that means brand voice docs, draft copy, and the product context —
nothing secret, but it does leave the machine, so it stays opt-in.
"""

import os
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()


def _tracing_requested():
    """Whether .env asks for tracing AND gives it what it needs to work.

    A key with no LANGSMITH_TRACING is someone who set up the account and hasn't
    switched it on. LANGSMITH_TRACING with no key is a typo away from every call
    failing to upload — better to stay off and say so than to retry uploads in
    the background of every request.
    """
    if os.getenv("LANGSMITH_TRACING", "").strip().lower() not in ("true", "1", "yes"):
        return False
    return bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))


ENABLED = False
_trace = None

if _tracing_requested():
    try:
        from langsmith.run_helpers import trace as _trace

        ENABLED = True
    except ImportError:
        # Configured but not installed. Say so once, at startup, rather than
        # letting someone conclude their traces are being recorded.
        print(
            "LANGSMITH_TRACING is set but the langsmith package isn't installed "
            "— running without tracing. Fix with: pip install langsmith",
            flush=True,
        )


def _usage(response):
    """Token counts from a google-genai response or stream chunk, if it has any.

    Returned under the key "usage_metadata", in LangSmith's own field names.
    That shape is required, not cosmetic: LangSmith populates its token and cost
    columns ONLY from outputs["usage_metadata"] with input_tokens/output_tokens/
    total_tokens. Logged under any other name, the numbers still show as output
    fields but every run reads "0 tokens" in the UI, which is worse than not
    logging them — it looks like an answer.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}

    prompt = getattr(usage, "prompt_token_count", None) or 0
    completion = getattr(usage, "candidates_token_count", None) or 0
    # Gemini reports thinking tokens separately, and they're billed as output.
    # LangSmith expects output_tokens to include them, with the split in details.
    reasoning = getattr(usage, "thoughts_token_count", None) or 0
    total = getattr(usage, "total_token_count", None) or (prompt + completion + reasoning)

    if not (prompt or completion or reasoning):
        return {}

    counts = {
        "input_tokens": prompt,
        "output_tokens": completion + reasoning,
        "total_tokens": total,
    }
    if reasoning:
        counts["output_token_details"] = {"reasoning": reasoning}
    return {"usage_metadata": counts}


class _NullSpan:
    """Stand-in when tracing is off, so call sites need no conditionals."""

    def record(self, response):
        pass

    def record_text(self, text, last_chunk=None):
        pass


class _Span:
    def __init__(self, run):
        self._run = run
        self._outputs = {}

    def record(self, response):
        """Log what a completed generate_content call returned."""
        finish_reason = None
        candidates = getattr(response, "candidates", None)
        if candidates:
            finish_reason = str(getattr(candidates[0], "finish_reason", None))

        # A response can legitimately have no text — a MALFORMED_FUNCTION_CALL or
        # a safety stop — and that case is exactly when a trace earns its keep,
        # so record the finish reason rather than only the text.
        self._outputs = {
            "text": getattr(response, "text", None),
            "finish_reason": finish_reason,
            **_usage(response),
        }

    def record_text(self, text, last_chunk=None):
        """Log assembled text, for streamed calls that have no single response.

        A stream's token counts arrive on its final chunk, so pass that in to get
        them — without it the span shows text and latency but no token usage.
        """
        self._outputs = {"text": text, **_usage(last_chunk)}

    def _end(self, error=None):
        if error is None:
            self._run.end(outputs=self._outputs)
        # On an error, langsmith records the exception from the context manager
        # itself; anything partial we have is still worth attaching.
        elif self._outputs:
            self._run.add_outputs(self._outputs)


@contextmanager
def model_span(name, prompt, model=None):
    """Trace one Gemini call. Yields a span to record the response on.

        with model_span("generate_draft", prompt, MODEL_NAME) as span:
            response = client.models.generate_content(...)
            span.record(response)

    The generate_content config is deliberately NOT logged. It's fixed per call
    site, so it says nothing a trace needs, and it isn't safely serializable —
    designer.py puts a live MCP session in there.

    Does nothing at all when tracing is off — see this module's docstring.
    """
    if not ENABLED:
        yield _NullSpan()
        return

    # ls_model_name / ls_provider are LangSmith's conventional keys — it matches
    # them against its price table to work out cost per run. It shows no cost for
    # a model it doesn't have pricing for, which is the case for newer Gemini
    # releases; the token counts are still exact either way.
    metadata = {"ls_provider": "google", "ls_model_name": model} if model else None
    with _trace(name=name, run_type="llm", inputs={"prompt": prompt},
                metadata=metadata) as run:
        span = _Span(run)
        try:
            yield span
        except BaseException:
            span._end(error=True)
            raise
        span._end()
