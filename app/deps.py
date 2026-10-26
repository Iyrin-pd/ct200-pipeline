"""Dependency wiring. Tests override these via app.dependency_overrides."""
from __future__ import annotations
from functools import lru_cache

from app.db import make_engine, make_session_factory
from app.llm.client import get_client
from app.stores.generation_store import make_store


@lru_cache
def _factory():
    return make_session_factory(make_engine())


def get_session():
    s = _factory()()
    try:
        yield s
    finally:
        s.close()


@lru_cache
def get_store():
    return make_store()


@lru_cache
def get_llm():
    return get_client()