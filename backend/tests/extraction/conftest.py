from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def fixture_dir(project_root: Path) -> Path:
    return project_root / "fixtures" / "contracts"


@pytest.fixture(scope="session")
def ground_truth(fixture_dir: Path) -> dict[str, Any]:
    return json.loads((fixture_dir / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture
def temp_fixture_dir(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "contracts"
    path.mkdir()
    yield path
