"""Transparent readers for plain and retention-compressed JSONL archives."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import IO


def jsonl_archive_paths(root: Path) -> list[Path]:
    return sorted((*root.glob("*/events.jsonl"), *root.glob("*/events.jsonl.gz")))


def open_jsonl_text(path: Path) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")
