from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from rescue_desk.domain.hashing import hash_payload


@dataclass(frozen=True)
class AuditPayload:
    organization_id: str
    actor_id: str
    action: str
    object_type: str
    object_id: str
    correlation_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime
    previous_hash: str | None


def calculate_event_hash(payload: AuditPayload) -> str:
    serializable = asdict(payload)
    created_at = payload.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = created_at.astimezone(UTC)
    serializable["created_at"] = created_at.isoformat()
    return hash_payload(serializable)


def verify_audit_chain(events: list[tuple[AuditPayload, str]]) -> bool:
    previous_hash: str | None = None
    for payload, stored_hash in events:
        if payload.previous_hash != previous_hash:
            return False
        if calculate_event_hash(payload) != stored_hash:
            return False
        previous_hash = stored_hash
    return True
