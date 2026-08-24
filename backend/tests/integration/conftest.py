from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from rescue_desk.auth import create_access_token, hash_password
from rescue_desk.config import get_settings
from rescue_desk.database import Base, get_db
from rescue_desk.main import app
from rescue_desk.models import Membership, Organization, Role, User


@dataclass(frozen=True)
class SeededIdentity:
    user_id: str
    organization_id: str
    headers: dict[str, str]


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Session]:
    monkeypatch.setenv("RESCUEDESK_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RESCUEDESK_ENABLE_LOCAL_AI", "false")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()

    def override_db() -> Generator[Session]:
        try:
            yield session
        except BaseException:
            # Match the production dependency: a handled HTTP exception still
            # has to unwind the request transaction before this shared test
            # session is reused by the next request.
            session.rollback()
            raise

    app.dependency_overrides[get_db] = override_db
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
def client(db: Session) -> Generator[TestClient]:
    del db
    with TestClient(app) as test_client:
        yield test_client


def seed_identity(
    db: Session,
    *,
    email: str,
    role: Role,
    organization_name: str = "Northstar Demo",
) -> SeededIdentity:
    organization = db.query(Organization).filter_by(name=organization_name).one_or_none()
    if organization is None:
        organization = Organization(name=organization_name)
        db.add(organization)
        db.flush()
    user = User(
        email=email,
        password_hash=hash_password("DemoPassword!2026"),
        display_name=email.split("@", maxsplit=1)[0].title(),
    )
    db.add(user)
    db.flush()
    membership = Membership(
        organization_id=organization.id,
        user_id=user.id,
        role=role,
    )
    db.add(membership)
    db.commit()
    token, _ = create_access_token(user=user, membership=membership)
    return SeededIdentity(
        user_id=user.id,
        organization_id=organization.id,
        headers={"Authorization": f"Bearer {token}"},
    )
