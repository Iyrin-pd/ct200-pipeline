"""Text normalisation + hashing shared by parser, matcher and staleness checks.

Two views of every string exist on purpose:
  raw  - exactly what the PDF said (kept for display and for the LLM)
  norm - canonical form used ONLY for hashing / comparing / matching
Normalisation must never be used to rewrite stored text.
"""
from __future__ import annotations
import hashlib
import re
import unicodedata

# Non-breaking / typographic hyphens -> ASCII hyphen. En/em dashes are NOT mapped,
# because "—" carries sentence meaning.
_HYPHENS = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2043\u00ad"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"'}
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = t.translate(_HYPHENS).translate(_QUOTES)
    return _WS.sub(" ", t).strip()


def normalize_heading(text: str) -> str:
    return normalize(text).casefold()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(heading: str, body_text: str) -> str:
    return sha256(normalize(heading) + "\x1f" + normalize(body_text))