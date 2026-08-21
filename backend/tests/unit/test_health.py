from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, create_engine, text

from rescue_desk import main
from rescue_desk.config import get_settings


def _engine_at_revision(revision: str) -> Engine:
    runtime_engine = create_engine("sqlite+pysqlite:///:memory:")
    with runtime_engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )
    return runtime_engine


def test_database_readiness_requires_exact_migration_head() -> None:
    expected_heads = main._expected_migration_heads()
    assert len(expected_heads) == 1
    runtime_engine = _engine_at_revision(expected_heads.pop())
    try:
        with runtime_engine.connect() as connection:
            main._assert_database_ready(connection)
    finally:
        runtime_engine.dispose()


def test_database_readiness_rejects_stale_migration() -> None:
    runtime_engine = _engine_at_revision("stale-revision")
    try:
        with (
            runtime_engine.connect() as connection,
            pytest.raises(main.ReadinessError, match="not at the application head"),
        ):
            main._assert_database_ready(connection)
    finally:
        runtime_engine.dispose()


def test_storage_probe_proves_writability_without_leaving_artifacts(tmp_path: Path) -> None:
    storage_directory = tmp_path / "uploads"
    storage_directory.mkdir()

    main._probe_writable_directory(storage_directory)

    assert list(storage_directory.iterdir()) == []


def test_ready_accepts_current_database_and_writable_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected_heads = main._expected_migration_heads()
    assert len(expected_heads) == 1
    runtime_engine = _engine_at_revision(expected_heads.pop())
    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setenv("RESCUEDESK_UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    storage_directories = main._runtime_storage_directories()
    for directory in storage_directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        assert main.ready() == {"status": "ready"}
        assert all(list(directory.iterdir()) == [] for directory in storage_directories)
    finally:
        runtime_engine.dispose()
        get_settings.cache_clear()


def test_ready_returns_service_unavailable_for_stale_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_engine = _engine_at_revision("stale-revision")
    monkeypatch.setattr(main, "engine", runtime_engine)
    monkeypatch.setenv("RESCUEDESK_UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    for directory in main._runtime_storage_directories():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        with pytest.raises(HTTPException) as captured:
            main.ready()
        assert captured.value.status_code == 503
        assert captured.value.detail == "Database migration or runtime storage is not ready"
    finally:
        runtime_engine.dispose()
        get_settings.cache_clear()
