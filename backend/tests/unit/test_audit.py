from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone

from rescue_desk.domain.audit import AuditPayload, calculate_event_hash, verify_audit_chain
from rescue_desk.domain.hashing import hash_payload


def payload(previous_hash: str | None, action: str) -> AuditPayload:
    return AuditPayload(
        organization_id="org-1",
        actor_id="user-1",
        action=action,
        object_type="case",
        object_id="case-1",
        correlation_id="corr-1",
        before=None,
        after={"status": action},
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
        previous_hash=previous_hash,
    )


def test_validates_hash_chain_and_detects_tampering() -> None:
    first = payload(None, "created")
    first_hash = calculate_event_hash(first)
    second = payload(first_hash, "reviewed")
    second_hash = calculate_event_hash(second)
    assert verify_audit_chain([(first, first_hash), (second, second_hash)])
    assert not verify_audit_chain([(first, first_hash), (second, "0" * 64)])


def test_hash_is_stable_after_timezone_loss_or_offset_conversion() -> None:
    aware = replace(
        payload(None, "created"),
        created_at=datetime(2026, 8, 20, 17, 4, 3, 123456, tzinfo=UTC),
    )
    sqlite_reloaded = replace(aware, created_at=aware.created_at.replace(tzinfo=None))
    equivalent_offset = replace(
        aware,
        created_at=datetime(
            2026,
            8,
            20,
            13,
            4,
            3,
            123456,
            tzinfo=timezone(-timedelta(hours=4)),
        ),
    )

    expected = calculate_event_hash(aware)
    assert calculate_event_hash(sqlite_reloaded) == expected
    assert calculate_event_hash(equivalent_offset) == expected

    legacy_serializable = asdict(aware)
    legacy_serializable["created_at"] = aware.created_at.isoformat()
    assert expected == hash_payload(legacy_serializable)
