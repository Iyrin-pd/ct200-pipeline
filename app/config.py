import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(ROOT / 'var' / 'ct200.db').as_posix()}")
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).resolve()       # path-ingest is confined here
MIN_SOURCE_CHARS = int(os.getenv("MIN_SOURCE_CHARS", "150"))          # below this we refuse to call the LLM
MAX_SELECTION_NODES = int(os.getenv("MAX_SELECTION_NODES", "60"))
# parse warnings that mean "the tree may be wrong/incomplete"; ingestion refuses them when strict
INTEGRITY_WARNINGS = {"content_loss", "content_added", "orphan_numbering", "duplicate_number", "missing_title"}