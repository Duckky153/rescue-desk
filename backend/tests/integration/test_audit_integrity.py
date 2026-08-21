from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from rescue_desk.auth import hash_password
from rescue_desk.database import Base
from rescue_desk.domain.audit import AuditPayload, calculate_event_hash, verify_audit_chain
from rescue_desk.models import AuditEvent, Organization, User
from rescue_desk.services.audit import append_audit_event


def _payload_from_event(event_record: AuditEvent) -> AuditPayload:
    return AuditPayload(
        organization_id=event_record.organization_id,
        actor_id=event_record.actor_id,
        action=event_record.action,
        object_type=event_record.object_type,
        object_id=event_record.object_id,
        correlation_id=event_record.correlation_id,
        before=event_record.before,
        after=event_record.after,
        created_at=event_record.created_at,
        previous_hash=event_record.previous_hash,
    )


def test_persisted_audit_hash_survives_database_reload(db: Session) -> None:
    organization = Organization(name="Audit Reload Organization")
    user = User(
        email="audit-reload@example.com",
        password_hash=hash_password("DemoPassword!2026"),
        display_name="Audit Reload",
    )
    db.add_all([organization, user])
    db.commit()

    append_audit_event(
        db,
        organization_id=organization.id,
        actor_id=user.id,
        action="case.created",
        object_type="case",
        object_id="case-reload",
        correlation_id="corr-reload",
        after={"status": "intake"},
    )
    db.commit()
    db.expire_all()

    stored = db.scalar(select(AuditEvent).where(AuditEvent.organization_id == organization.id))
    assert stored is not None
    assert calculate_event_hash(_payload_from_event(stored)) == stored.event_hash


def test_multiple_appends_in_one_transaction_extend_the_same_chain(db: Session) -> None:
    organization = Organization(name="Audit Batch Organization")
    user = User(
        email="audit-batch@example.com",
        password_hash=hash_password("DemoPassword!2026"),
        display_name="Audit Batch",
    )
    db.add_all([organization, user])
    db.commit()

    first = append_audit_event(
        db,
        organization_id=organization.id,
        actor_id=user.id,
        action="case.created",
        object_type="case",
        object_id="case-batch",
        correlation_id="corr-batch-1",
    )
    second = append_audit_event(
        db,
        organization_id=organization.id,
        actor_id=user.id,
        action="case.reviewed",
        object_type="case",
        object_id="case-batch",
        correlation_id="corr-batch-2",
    )
    assert second.created_at > first.created_at
    db.commit()

    records = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization.id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    )
    chain = [(_payload_from_event(record), record.event_hash) for record in records]
    assert len(records) == 2
    assert verify_audit_chain(chain)


def test_concurrent_appends_keep_one_serial_hash_chain(tmp_path: Path) -> None:
    database_path = tmp_path / "audit-concurrency.db"
    runtime_engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    session_factory = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    Base.metadata.create_all(runtime_engine)
    with session_factory() as seed_session:
        organization = Organization(name="Concurrent Audit Organization")
        user = User(
            email="audit-concurrency@example.com",
            password_hash=hash_password("DemoPassword!2026"),
            display_name="Audit Concurrency",
        )
        seed_session.add_all([organization, user])
        seed_session.commit()
        organization_id = organization.id
        user_id = user.id

    first_has_lock = Event()
    second_update_attempted = Event()
    release_first = Event()

    @event.listens_for(runtime_engine, "before_cursor_execute")
    def observe_second_lock_attempt(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        is_organization_update = statement.lstrip().upper().startswith("UPDATE ORGANIZATIONS")
        if first_has_lock.is_set() and is_organization_update:
            second_update_attempted.set()

    def append_first() -> None:
        with session_factory() as session:
            append_audit_event(
                session,
                organization_id=organization_id,
                actor_id=user_id,
                action="case.created",
                object_type="case",
                object_id="case-concurrent",
                correlation_id="corr-first",
            )
            first_has_lock.set()
            assert release_first.wait(timeout=5)
            session.commit()

    def append_second() -> None:
        with session_factory() as session:
            append_audit_event(
                session,
                organization_id=organization_id,
                actor_id=user_id,
                action="case.reviewed",
                object_type="case",
                object_id="case-concurrent",
                correlation_id="corr-second",
            )
            session.commit()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(append_first)
            assert first_has_lock.wait(timeout=5)
            second_future = executor.submit(append_second)
            assert second_update_attempted.wait(timeout=5)
            assert not second_future.done()
            release_first.set()
            first_future.result(timeout=5)
            second_future.result(timeout=5)

        with session_factory() as verification_session:
            records = list(
                verification_session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.organization_id == organization_id)
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            )
            chain = [(_payload_from_event(record), record.event_hash) for record in records]
            assert len(records) == 2
            assert verify_audit_chain(chain)
    finally:
        release_first.set()
        event.remove(runtime_engine, "before_cursor_execute", observe_second_lock_attempt)
        Base.metadata.drop_all(runtime_engine)
        runtime_engine.dispose()
