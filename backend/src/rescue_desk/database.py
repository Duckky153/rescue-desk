from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from rescue_desk.config import get_settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict[str, object]:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


settings = get_settings()
engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def register_rollback_path(session: Session, path: Path) -> None:
    """Remove a newly written runtime file if its database transaction rolls back."""

    paths = session.info.setdefault("rollback_paths", set())
    if not isinstance(paths, set):
        raise RuntimeError("Session rollback path registry is invalid")
    paths.add(path)


@event.listens_for(Session, "after_rollback")
def _remove_rolled_back_files(session: Session) -> None:
    paths = session.info.pop("rollback_paths", set())
    if isinstance(paths, set):
        for raw_path in paths:
            if isinstance(raw_path, Path):
                raw_path.unlink(missing_ok=True)


@event.listens_for(Session, "after_commit")
def _forget_committed_files(session: Session) -> None:
    session.info.pop("rollback_paths", None)


def get_db() -> Generator[Session]:
    session = SessionLocal()
    try:
        yield session
    except BaseException:
        # FastAPI resumes yielded dependencies with the request exception.  An
        # explicit rollback is required here so idempotency reservations and
        # staged runtime files cannot leak out of a failed request.
        session.rollback()
        raise
    finally:
        session.close()
