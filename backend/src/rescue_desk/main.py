import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from rescue_desk.config import get_settings
from rescue_desk.database import engine
from rescue_desk.domain.calculations import CalculationInputError
from rescue_desk.domain.workflow import InvalidTransitionError
from rescue_desk.routers import auth, cases, documents, exports

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class ReadinessError(RuntimeError):
    """A runtime dependency is reachable but not safe to serve traffic."""


def _runtime_storage_directories() -> tuple[Path, Path]:
    configured_upload_root = get_settings().upload_dir
    upload_root = configured_upload_root.resolve()
    export_root = (configured_upload_root.parent / "exports").resolve()
    return upload_root, export_root


def _expected_migration_heads() -> set[str]:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return set(ScriptDirectory.from_config(config).get_heads())


def _assert_database_ready(connection: Connection) -> None:
    connection.execute(text("SELECT 1"))
    expected_heads = _expected_migration_heads()
    current_heads = set(MigrationContext.configure(connection).get_current_heads())
    if not expected_heads or current_heads != expected_heads:
        raise ReadinessError("The database migration revision is not at the application head")


def _probe_writable_directory(directory: Path) -> None:
    if not directory.is_dir():
        raise ReadinessError("A runtime storage directory is missing")

    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=".rescuedesk-ready-",
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            os.chmod(probe_path, 0o600)
            probe.write(b"ready")
            probe.flush()
            os.fsync(probe.fileno())
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    for directory in _runtime_storage_directories():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="RescueDesk API",
    version="0.1.0",
    description=(
        "Evidence-backed ERP contract exit review. Demonstration only; not legal, "
        "accounting, or financial advice; not affiliated with Entry Inc."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Correlation-ID"],
)
app.include_router(auth.router)
app.include_router(cases.router)
app.include_router(documents.router)
app.include_router(exports.router)


@app.exception_handler(InvalidTransitionError)
async def invalid_transition_handler(_: Request, exc: InvalidTransitionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(CalculationInputError)
async def calculation_input_handler(_: Request, exc: CalculationInputError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            _assert_database_ready(connection)
        for directory in _runtime_storage_directories():
            _probe_writable_directory(directory)
    except (CommandError, OSError, RuntimeError, SQLAlchemyError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Database migration or runtime storage is not ready",
        ) from exc
    return {"status": "ready"}
