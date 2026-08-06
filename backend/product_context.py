"""Product context — the org's internal product handover doc (product.pdf),
loaded once and handed to content-writing agents alongside brand_voice.md.

brand_voice.md carries tone, positioning, and a short product summary;
product.pdf carries the depth (what's live, shipping, or still planned, plus
specifics brand_voice.md never mentions) so a draft doesn't get the org's own
products wrong. Read-only and best-effort: if the file is missing or fails to
parse, callers get back an empty string and fall back to brand_voice.md alone
rather than failing the whole request.
"""

import re
from pathlib import Path

from pypdf import PdfReader

PRODUCT_DOC_PATH = Path(__file__).resolve().parent.parent / "product.pdf"

_cache = None


def load_product_context():
    global _cache
    if _cache is None:
        _cache = _extract_text()
    return _cache


def _extract_text():
    if not PRODUCT_DOC_PATH.exists():
        return ""
    try:
        reader = PdfReader(str(PRODUCT_DOC_PATH))
        raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""
    # The source PDF's text layer breaks mid-word onto its own line for long
    # stretches — collapsing all whitespace runs to a single space keeps the
    # doc readable to the model without carrying that noise into the prompt.
    text = re.sub(r"\s+", " ", raw).strip()

    # This is an internal handover doc and carries staff contact emails. The
    # prompt instructs agents never to surface them, but stripping them here
    # too means a leak can't happen even if a model ignores that instruction.
    return re.sub(r"[\w.+-]+@[\w.-]+\.\w+", "[redacted]", text)
