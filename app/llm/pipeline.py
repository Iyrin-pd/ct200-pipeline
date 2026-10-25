"""Orchestrates: build prompt -> call model -> validate -> (one repair attempt) -> decide.

Failure policy:
  * Max 2 attempts. Attempt 2 is a REPAIR prompt that tells the model exactly what was wrong.
  * Transport errors (timeout/5xx/no key) consume an attempt; no sleeping inside a request.
  * We keep the BEST attempt (most valid cases), never a mix, so provenance stays simple.
  * >=3 valid & clean            -> 'succeeded'
    >=3 valid but some dropped   -> 'partial'  (dropped items listed with reasons)
    <3 valid                     -> 'failed'   (raw replies kept for audit; ZERO test cases
                                                exposed - unvalidated output is never served)
"""
from __future__ import annotations
from dataclasses import dataclass, field

from app.llm.client import LLMClient, LLMTransportError
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.llm.validation import Outcome, classify_outcome, parse_response

MAX_ATTEMPTS = 2


@dataclass
class PipelineResult:
    status: str                                   # succeeded | partial | failed
    cases: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    error: str | None = None


def _rank(o: Outcome) -> tuple[int, int]:
    return ({"succeeded": 2, "partial": 1, "unusable": 0}[classify_outcome(o)], len(o.cases))


def run(client: LLMClient, sources: list[dict]) -> PipelineResult:
    attempts: list[dict] = []
    best: Outcome | None = None
    problems: str | None = None
    last_error: str | None = None
    for n in range(1, MAX_ATTEMPTS + 1):
        user = build_user_prompt(sources, problems)
        rec = {"attempt": n, "repair": bool(problems), "raw": None, "truncated": False, "transport_error": None}
        try:
            reply = client.complete(SYSTEM_PROMPT, user)
        except LLMTransportError as e:
            rec["transport_error"] = last_error = str(e)
            attempts.append(rec)
            problems = "- the previous request failed to return a reply"
            continue
        rec["raw"], rec["truncated"] = reply.text, reply.truncated
        outcome = parse_response(reply.text, sources, reply.truncated)
        rec["valid"], rec["rejected"], rec["fatal"] = len(outcome.cases), len(outcome.rejected), outcome.fatal
        attempts.append(rec)
        if best is None or _rank(outcome) > _rank(best):
            best = outcome
        if classify_outcome(outcome) == "succeeded":
            break
        problems = outcome.problems
    if best is None:
        return PipelineResult("failed", attempts=attempts, error=f"LLM unavailable: {last_error}")
    verdict = classify_outcome(best)
    if verdict == "unusable":
        return PipelineResult("failed", rejected=best.rejected, notes=best.notes, attempts=attempts,
                              error=f"only {len(best.cases)} valid test case(s) after {len(attempts)} attempt(s): "
                                    f"{best.fatal or best.problems}")
    return PipelineResult(verdict, best.cases, best.rejected, best.notes, attempts)