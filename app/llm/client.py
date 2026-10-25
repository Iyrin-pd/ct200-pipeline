"""LLM provider abstraction. The pipeline only knows `LLMClient.complete()`.

  mock           - deterministic, offline, grounded (default; the system and its tests run
                   without an API key).
  openai_compat  - any OpenAI-compatible endpoint: Groq, Gemini, OpenRouter, ...
  ScriptedClient - test double that replays canned replies / raises canned errors.
"""
from __future__ import annotations
import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

from app.llm.prompt import TYPES


@dataclass
class LLMReply:
    text: str
    truncated: bool = False          # provider says it hit the token limit
    model: str = ""


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str) -> LLMReply: ...


class LLMTransportError(RuntimeError):
    """Timeouts, HTTP errors, rate limits, missing config: nothing came back to validate."""


# ----------------------------------------------------------------------------- real provider
class OpenAICompatClient:
    """Works with any provider exposing POST {base_url}/chat/completions.

    Env vars: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, optional LLM_JSON_MODE (default true).
    JSON mode only *asks* the provider for JSON; validation still runs on every reply.
    """
    provider = "openai_compat"

    def __init__(self, timeout: float = 60.0, max_tokens: int = 2500):
        self.model = os.getenv("LLM_MODEL", "unset")
        self.timeout, self.max_tokens = timeout, max_tokens

    def complete(self, system: str, user: str) -> LLMReply:
        base = os.getenv("LLM_BASE_URL", "").rstrip("/")
        key = os.getenv("LLM_API_KEY", "")
        if not base or not key or self.model == "unset":
            raise LLMTransportError("LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must all be set")
        body = {"model": self.model, "temperature": 0.2, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if os.getenv("LLM_JSON_MODE", "true").lower() != "false":
            body["response_format"] = {"type": "json_object"}
        try:
            import httpx
            r = httpx.post(f"{base}/chat/completions", json=body, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {key}"})
        except Exception as e:                      # network error, timeout, DNS...
            raise LLMTransportError(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise LLMTransportError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            choice = r.json()["choices"][0]
            text = choice["message"].get("content") or ""
            truncated = choice.get("finish_reason") == "length"
        except (KeyError, IndexError, ValueError, AttributeError) as e:
            raise LLMTransportError(f"unexpected response shape: {r.text[:200]}") from e
        return LLMReply(text=text, truncated=truncated, model=self.model)


# ----------------------------------------------------------------------------- offline provider
_SEC = re.compile(r'<section id="(\d+)" number="([^"]*)" title="([^"]*)">\n(.*?)\n</section>', re.S)
_SENT = re.compile(r"(?<=[.;])\s+|\n+")


class MockClient:
    """Deterministic, grounded generator: quotes real sentences/table rows from the excerpts."""
    provider = "mock"
    model = "mock-heuristic-1"

    def complete(self, system: str, user: str) -> LLMReply:
        secs = [(int(i), n, h, body) for i, n, h, body in _SEC.findall(user)]
        picks = []
        for nid, num, head, body in secs:
            for frag in (f.strip() for f in _SENT.split(body)):
                frag = re.sub(r"^\|\s*|\s*\|$", "", frag) if frag.startswith("|") else frag
                if len(frag) >= 12 and not set(frag) <= {"-", "|", " "}:
                    picks.append((bool(re.search(r"\d", frag)), nid, head, frag[:180]))
        picks.sort(key=lambda t: (not t[0],))            # sentences containing numbers first
        cases = []
        for k, (_, nid, head, quote) in enumerate(picks[:5]):
            cases.append({
                "title": f"Verify {head.lower()}: {quote[:50].rstrip()}",
                "type": TYPES[k % len(TYPES)], "priority": "high" if re.search(r"\d", quote) else "medium",
                "preconditions": ["Device powered on with fresh batteries"],
                "steps": [f"Exercise the behaviour described in section '{head}'", "Observe the device"],
                "expected_result": f"Device behaviour matches the manual: {quote}",
                "source_node_ids": [nid], "evidence": [{"node_id": nid, "quote": quote}]})
        return LLMReply(json.dumps({"test_cases": cases}), model=self.model)


# ----------------------------------------------------------------------------- test double
class ScriptedClient:
    provider = "scripted"
    model = "scripted-1"

    def __init__(self, replies: list):
        self.replies, self.calls = list(replies), []

    def complete(self, system: str, user: str) -> LLMReply:
        self.calls.append(user)
        if not self.replies:
            raise LLMTransportError("scripted client exhausted")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, LLMReply) else LLMReply(r, model=self.model)


def get_client(provider: str | None = None) -> LLMClient:
    provider = (provider or os.getenv("LLM_PROVIDER", "mock")).lower()
    if provider == "openai_compat":
        return OpenAICompatClient()
    return MockClient()