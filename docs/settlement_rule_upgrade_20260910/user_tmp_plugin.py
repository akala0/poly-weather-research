"""Test-only tmp_path fixture using an app-writable directory; no product code."""
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path("C:/Users/Administrator/.codex/visualizations/2026/09/10/01a0893c-f337-7e93-b433-433021199517")


@pytest.fixture
def tmp_path(request):
    path = ROOT / ("pytest-user-" + uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    return path
