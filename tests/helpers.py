from functools import lru_cache
from tests.conftest import V1, V2
from app.parser.pdf_parser import parse_pdf


@lru_cache(maxsize=None)
def doc(version: int):
    return parse_pdf(V1 if version == 1 else V2)


def node(d, number, heading=None):
    ns = [n for n in d.nodes if n.number == number and (heading is None or n.heading == heading)]
    assert len(ns) == 1, f"expected exactly one node {number}, got {len(ns)}"
    return ns[0]