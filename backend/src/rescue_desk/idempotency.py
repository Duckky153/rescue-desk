from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from rescue_desk.domain.hashing import hash_payload
from rescue_desk.models import IdempotencyRecord


def payload_hash(value: Any) -> str:
    return hash_payload(value)


def replay_if_present(
    db: Session,
    *,
    organization_id: str,
    endpoint: str,
    key: str,
    request_hash: str,
) -> IdempotencyRecord | None:
    """Atomically reserve a key, or return its completed response.

    The reservation is inserted inside the caller's business transaction. A crash or
    rollback therefore removes both the reservation and every database side effect.
    """

    values = {
        "organization_id": organization_id,
        "endpoint": endpoint,
        "key": key,
        "request_hash": request_hash,
        "response_status": 0,
        "response_body": {},
    }
    conflict_columns = [
        IdempotencyRecord.organization_id,
        IdempotencyRecord.endpoint,
        IdempotencyRecord.key,
    ]
    dialect_name = db.get_bind().dialect.name
    reservation: Insert
    if dialect_name == "sqlite":
        reservation = (
            sqlite_insert(IdempotencyRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
        )
    elif dialect_name == "postgresql":
        reservation = (
            postgresql_insert(IdempotencyRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
        )
    else:
        raise RuntimeError(
            f"Database dialect {dialect_name!r} does not support atomic idempotency reservations"
        )

    inserted_id = db.scalar(reservation.returning(IdempotencyRecord.id))
    if inserted_id is not None:
        return None

    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.organization_id == organization_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.key == key,
        )
    )
    if record is None:
        raise RuntimeError("Idempotency reservation conflict could not be reloaded")
    if record.request_hash != request_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used with a different request",
        )
    if record.response_status == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An identical request is still being processed",
        )
    return record


def store_response(
    db: Session,
    *,
    organization_id: str,
    endpoint: str,
    key: str,
    request_hash: str,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    record = db.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.organization_id == organization_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.key == key,
        )
        .with_for_update()
    )
    if record is None or record.request_hash != request_hash:
        raise RuntimeError("Idempotency reservation is missing or inconsistent")
    record.response_status = response_status
    record.response_body = response_body
    db.commit()
