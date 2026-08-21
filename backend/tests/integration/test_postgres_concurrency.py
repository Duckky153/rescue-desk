from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import rescue_desk.services.documents as documents_service
import rescue_desk.services.exports as export_service
import rescue_desk.services.workbench as workbench_service
from rescue_desk.auth import Principal, create_access_token, hash_password
from rescue_desk.config import get_settings
from rescue_desk.database import Base, get_db
from rescue_desk.domain.money import MAX_MINOR_UNITS
from rescue_desk.main import app
from rescue_desk.models import (
    AssertionReviewState,
    AssertionSource,
    AuditEvent,
    CaseRevision,
    ContractAssertion,
    ContractDocument,
    DocumentPage,
    EvidenceSpan,
    ExportArtifact,
    ExportKind,
    ExtractionRun,
    FeeCategory,
    FeeObligation,
    IdempotencyRecord,
    Membership,
    Organization,
    ProcessingStatus,
    RescueCase,
    ReviewDecision,
    ReviewDecisionType,
    Role,
    SafetyStatus,
    User,
)
from rescue_desk.schemas import AssertionReviewRequest, FeeCreate, TransitionRequest
from rescue_desk.services.calculations import run_calculation
from rescue_desk.services.cases import transition_case
from rescue_desk.services.exports import (
    create_export,
    prepare_download,
    verify_export_artifact_integrity,
)
from rescue_desk.services.review import add_fee, review_assertion
from rescue_desk.services.workbench import build_workbench_snapshot

POSTGRES_TEST_URL_ENV = "RESCUEDESK_TEST_POSTGRES_URL"
FIXTURES = Path(__file__).parents[3] / "fixtures" / "contracts"


@dataclass(frozen=True)
class PostgresHarness:
    engine: Engine
    sessions: sessionmaker[Session]
    upload_dir: Path


@dataclass(frozen=True)
class SeededCase:
    case_id: str
    revision_id: str
    calculation_id: str | None
    fee_assertion_id: str
    analyst: Principal
    approver: Principal


def _truncate_application_tables(engine: Engine) -> None:
    names = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def postgres_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PostgresHarness]:
    url = os.environ.get(POSTGRES_TEST_URL_ENV)
    if not url:
        pytest.skip(f"Set {POSTGRES_TEST_URL_ENV} to an isolated migrated PostgreSQL database")
    database_name = make_url(url).database or ""
    if not database_name.endswith("_contract_test"):
        pytest.fail("PostgreSQL concurrency tests require an isolated *_contract_test database")
    engine = create_engine(url, pool_pre_ping=True)
    _truncate_application_tables(engine)
    upload_dir = tmp_path / "uploads"
    monkeypatch.setenv("RESCUEDESK_UPLOAD_DIR", str(upload_dir))
    get_settings.cache_clear()
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield PostgresHarness(engine=engine, sessions=sessions, upload_dir=upload_dir)
    finally:
        _truncate_application_tables(engine)
        engine.dispose()
        get_settings.cache_clear()


def _sha256(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(raw).hexdigest()


def _seed_case(
    harness: PostgresHarness,
    *,
    include_fee: bool,
    include_calculation: bool,
) -> SeededCase:
    with harness.sessions() as db:
        suffix = uuid.uuid4().hex
        organization = Organization(name=f"PostgreSQL concurrency {suffix}")
        analyst_user = User(
            email=f"analyst-{suffix}@example.com",
            password_hash="not-used-in-service-test",
            display_name="Concurrency Analyst",
        )
        approver_user = User(
            email=f"approver-{suffix}@example.com",
            password_hash="not-used-in-service-test",
            display_name="Concurrency Approver",
        )
        db.add_all([organization, analyst_user, approver_user])
        db.flush()
        db.add_all(
            [
                Membership(
                    organization_id=organization.id,
                    user_id=analyst_user.id,
                    role=Role.ANALYST,
                ),
                Membership(
                    organization_id=organization.id,
                    user_id=approver_user.id,
                    role=Role.APPROVER,
                ),
            ]
        )
        case = RescueCase(
            organization_id=organization.id,
            display_name="PostgreSQL lifecycle race",
            applicant_company="Northstar Synthetic",
            erp_provider="LegacySuite Synthetic",
            assigned_analyst_id=analyst_user.id,
            assigned_approver_id=approver_user.id,
        )
        db.add(case)
        db.flush()
        revision = CaseRevision(
            case_id=case.id,
            number=1,
            reason="Initial concurrency revision",
            created_by=analyst_user.id,
        )
        db.add(revision)
        db.flush()

        contract_quote = "Contract ends on December 31, 2027."
        fee_quote = "Annual subscription fee is USD 12,000."
        page_text = f"{contract_quote}\n{fee_quote}"
        stored_filename = f"pg-concurrency-{suffix}.pdf"
        harness.upload_dir.mkdir(parents=True, exist_ok=True)
        (harness.upload_dir / stored_filename).write_bytes(page_text.encode())
        document = ContractDocument(
            organization_id=organization.id,
            case_revision_id=revision.id,
            original_filename="synthetic-contract.pdf",
            stored_filename=stored_filename,
            sha256=_sha256(page_text),
            media_type="application/pdf",
            source_type="synthetic",
            size_bytes=len(page_text.encode()),
            page_count=1,
            safety_status=SafetyStatus.PASSED,
            processing_status=ProcessingStatus.PROCESSED,
            created_by=analyst_user.id,
        )
        db.add(document)
        db.flush()
        page = DocumentPage(
            document_id=document.id,
            page_number=1,
            text=page_text,
            text_sha256=_sha256(page_text),
            extraction_confidence=1,
        )
        db.add(page)
        db.flush()
        contract_span = EvidenceSpan(
            page_id=page.id,
            quote=contract_quote,
            char_start=0,
            char_end=len(contract_quote),
            quote_sha256=_sha256(contract_quote),
        )
        fee_start = page_text.index(fee_quote)
        fee_span = EvidenceSpan(
            page_id=page.id,
            quote=fee_quote,
            char_start=fee_start,
            char_end=fee_start + len(fee_quote),
            quote_sha256=_sha256(fee_quote),
        )
        db.add_all([contract_span, fee_span])
        db.flush()
        contract_assertion = ContractAssertion(
            case_revision_id=revision.id,
            semantic_key="contract_end_date",
            raw_value="2027-12-31",
            normalized_value={"type": "date", "value": "2027-12-31"},
            display_value="2027-12-31",
            source=AssertionSource.HUMAN,
            confidence=1,
            review_state=AssertionReviewState.ACCEPTED,
            evidence=[contract_span],
        )
        fee_assertion = ContractAssertion(
            case_revision_id=revision.id,
            semantic_key="fee.subscription",
            raw_value="USD 12,000",
            normalized_value={
                "type": "money",
                "amount_minor": 1_200_000,
                "currency": "USD",
                "cadence": "annual",
            },
            display_value="USD 12,000.00",
            source=AssertionSource.HUMAN,
            confidence=1,
            review_state=AssertionReviewState.ACCEPTED,
            evidence=[fee_span],
        )
        db.add_all([contract_assertion, fee_assertion])
        db.flush()
        db.add_all(
            [
                ReviewDecision(
                    assertion_id=assertion.id,
                    decision=ReviewDecisionType.ACCEPT,
                    reviewer_id=analyst_user.id,
                    reason="Verified exact source evidence for concurrency fixture",
                    assumption=False,
                )
                for assertion in (contract_assertion, fee_assertion)
            ]
        )
        if include_fee:
            db.add(
                FeeObligation(
                    case_revision_id=revision.id,
                    primary_money_assertion_id=fee_assertion.id,
                    category=FeeCategory.SUBSCRIPTION,
                    amount_minor=1_200_000,
                    currency="USD",
                    service_start=date(2026, 1, 1),
                    service_end=date(2027, 1, 1),
                    payment_status="unpaid",
                    billing_cadence="annual",
                    proration_rule="contract_daily",
                    assertion_ids=[fee_assertion.id],
                    reviewed=True,
                )
            )
        db.commit()
        analyst = Principal(
            user_id=analyst_user.id,
            organization_id=organization.id,
            role=Role.ANALYST,
            email=analyst_user.email,
            display_name=analyst_user.display_name,
        )
        approver = Principal(
            user_id=approver_user.id,
            organization_id=organization.id,
            role=Role.APPROVER,
            email=approver_user.email,
            display_name=approver_user.display_name,
        )
        calculation_id: str | None = None
        if include_calculation:
            calculation = run_calculation(
                db,
                principal=analyst,
                case_id=case.id,
                as_of_date=date(2026, 7, 2),
                correlation_id=f"pg-seed-calc-{suffix}",
            )
            calculation_id = calculation.id
            db.commit()
        return SeededCase(
            case_id=case.id,
            revision_id=revision.id,
            calculation_id=calculation_id,
            fee_assertion_id=fee_assertion.id,
            analyst=analyst,
            approver=approver,
        )


def _seed_empty_case(harness: PostgresHarness, principal: Principal) -> str:
    with harness.sessions() as db:
        case = RescueCase(
            organization_id=principal.organization_id,
            display_name="Aggregate race",
            applicant_company="Synthetic Applicant",
            erp_provider="Synthetic ERP",
            assigned_analyst_id=principal.user_id,
        )
        db.add(case)
        db.flush()
        db.add(
            CaseRevision(
                case_id=case.id,
                number=1,
                reason="Aggregate race revision",
                created_by=principal.user_id,
            )
        )
        db.commit()
        return case.id


def _reviewed_fee(assertion_id: str) -> FeeCreate:
    return FeeCreate(
        category=FeeCategory.SUBSCRIPTION,
        amount_minor=1_200_000,
        currency="USD",
        service_start=date(2026, 1, 1),
        service_end=date(2027, 1, 1),
        payment_status="unpaid",
        billing_cadence="annual",
        proration_rule="contract_daily",
        assertion_ids=[assertion_id],
        reviewed=True,
    )


def _unreviewed_fee(amount_minor: int) -> FeeCreate:
    return FeeCreate(
        category=FeeCategory.OTHER,
        amount_minor=amount_minor,
        currency="USD",
        payment_status="paid",
        assertion_ids=[],
        reviewed=False,
    )


def test_postgres_serializes_primary_evidence_and_aggregate_fee_races(
    postgres_harness: PostgresHarness,
) -> None:
    seeded = _seed_case(postgres_harness, include_fee=False, include_calculation=False)
    barrier = Barrier(2)

    def create_reviewed(index: int) -> tuple[str, int | str]:
        with postgres_harness.sessions() as db:
            barrier.wait(timeout=10)
            try:
                fee = add_fee(
                    db,
                    principal=seeded.analyst,
                    case_id=seeded.case_id,
                    payload=_reviewed_fee(seeded.fee_assertion_id),
                    correlation_id=f"pg-primary-race-{index}",
                )
                db.commit()
                return ("created", fee.id)
            except HTTPException as exc:
                db.rollback()
                return ("rejected", exc.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        primary_results = list(pool.map(create_reviewed, range(2)))
    assert sorted(value[0] for value in primary_results) == ["created", "rejected"]
    assert next(value[1] for value in primary_results if value[0] == "rejected") == 409
    with postgres_harness.sessions() as db:
        active_primary = list(
            db.scalars(
                select(FeeObligation).where(
                    FeeObligation.case_revision_id == seeded.revision_id,
                    FeeObligation.superseded.is_(False),
                )
            )
        )
        assert len(active_primary) == 1
        assert active_primary[0].primary_money_assertion_id == seeded.fee_assertion_id
        db.add(
            ReviewDecision(
                assertion_id=seeded.fee_assertion_id,
                decision=ReviewDecisionType.REJECT,
                reviewer_id=seeded.analyst.user_id,
                reason="Valid PostgreSQL JSON SQL-NULL regression",
                corrected_value=None,
                assumption=False,
            )
        )
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO review_decisions "
                    "(id, assertion_id, decision, reviewer_id, reason, corrected_value, "
                    "assumption, created_at) VALUES "
                    "(:id, :assertion_id, 'ACCEPT', :reviewer_id, 'Invalid JSON payload', "
                    "CAST('{}' AS JSON), FALSE, now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "assertion_id": seeded.fee_assertion_id,
                    "reviewer_id": seeded.analyst.user_id,
                },
            )
        db.rollback()

    aggregate_case_id = _seed_empty_case(postgres_harness, seeded.analyst)
    aggregate_barrier = Barrier(2)

    def create_aggregate(index_and_amount: tuple[int, int]) -> tuple[str, int]:
        index, amount = index_and_amount
        with postgres_harness.sessions() as db:
            aggregate_barrier.wait(timeout=10)
            try:
                add_fee(
                    db,
                    principal=seeded.analyst,
                    case_id=aggregate_case_id,
                    payload=_unreviewed_fee(amount),
                    correlation_id=f"pg-aggregate-race-{index}",
                )
                db.commit()
                return ("created", amount)
            except HTTPException as exc:
                db.rollback()
                return ("rejected", exc.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        aggregate_results = list(pool.map(create_aggregate, enumerate((MAX_MINOR_UNITS, 1))))
    assert sorted(value[0] for value in aggregate_results) == ["created", "rejected"]
    assert next(value[1] for value in aggregate_results if value[0] == "rejected") == 422
    with postgres_harness.sessions() as db:
        amounts = list(
            db.scalars(
                select(FeeObligation.amount_minor)
                .join(CaseRevision, CaseRevision.id == FeeObligation.case_revision_id)
                .where(
                    CaseRevision.case_id == aggregate_case_id,
                    FeeObligation.superseded.is_(False),
                )
            )
        )
        assert len(amounts) == 1
        assert sum(amounts) <= MAX_MINOR_UNITS


def test_postgres_review_and_fee_use_one_case_first_lock_order(
    postgres_harness: PostgresHarness,
) -> None:
    seeded = _seed_case(postgres_harness, include_fee=False, include_calculation=False)
    with postgres_harness.sessions() as db:
        assertion = db.get(ContractAssertion, seeded.fee_assertion_id)
        assert assertion is not None
        db.query(ReviewDecision).filter_by(assertion_id=assertion.id).delete()
        assertion.review_state = AssertionReviewState.PROPOSED
        db.commit()

    barrier = Barrier(2)

    def accept_assertion() -> tuple[str, int | str]:
        with postgres_harness.sessions() as db:
            barrier.wait(timeout=10)
            try:
                reviewed = review_assertion(
                    db,
                    principal=seeded.analyst,
                    assertion_id=seeded.fee_assertion_id,
                    payload=AssertionReviewRequest(
                        decision=ReviewDecisionType.ACCEPT,
                        expected_assertion_version=1,
                        reason="Concurrent reviewer verified the exact source quote",
                    ),
                    correlation_id="pg-review-fee-lock-review",
                )
                db.commit()
                return ("reviewed", reviewed.id)
            except HTTPException as exc:
                db.rollback()
                return ("rejected", exc.status_code)

    def create_fee() -> tuple[str, int | str]:
        with postgres_harness.sessions() as db:
            barrier.wait(timeout=10)
            try:
                fee = add_fee(
                    db,
                    principal=seeded.analyst,
                    case_id=seeded.case_id,
                    payload=_reviewed_fee(seeded.fee_assertion_id),
                    correlation_id="pg-review-fee-lock-fee",
                )
                db.commit()
                return ("created", fee.id)
            except HTTPException as exc:
                db.rollback()
                return ("rejected", exc.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        review_future = pool.submit(accept_assertion)
        fee_future = pool.submit(create_fee)
        review_result = review_future.result(timeout=20)
        fee_result = fee_future.result(timeout=20)
    assert review_result[0] == "reviewed"
    assert fee_result[0] in {"created", "rejected"}
    if fee_result[0] == "rejected":
        assert fee_result[1] == 422

    with postgres_harness.sessions() as db:
        assertion = db.get(ContractAssertion, seeded.fee_assertion_id)
        assert assertion is not None
        assert assertion.review_state == AssertionReviewState.ACCEPTED
        active_fees = list(
            db.scalars(
                select(FeeObligation).where(
                    FeeObligation.case_revision_id == seeded.revision_id,
                    FeeObligation.superseded.is_(False),
                )
            )
        )
        assert len(active_fees) <= 1


def test_postgres_serializes_same_kind_export_and_reopen_races(
    postgres_harness: PostgresHarness,
) -> None:
    seeded = _seed_case(postgres_harness, include_fee=True, include_calculation=True)
    assert seeded.calculation_id is not None
    barrier = Barrier(2)

    def generate(index: int) -> str:
        with postgres_harness.sessions() as db:
            barrier.wait(timeout=10)
            artifact = create_export(
                db,
                principal=seeded.analyst,
                case_id=seeded.case_id,
                kind=ExportKind.MACHINE_READABLE_JSON,
                calculation_id=seeded.calculation_id,
                correlation_id=f"pg-export-race-{index}",
            )
            artifact_id = artifact.id
            db.commit()
            return artifact_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        artifact_ids = list(pool.map(generate, range(2)))
    assert len(set(artifact_ids)) == 2
    with postgres_harness.sessions() as db:
        artifacts = list(
            db.scalars(select(ExportArtifact).where(ExportArtifact.id.in_(artifact_ids)))
        )
        assert len(artifacts) == 2
        assert sum(not artifact.superseded for artifact in artifacts) == 1
        for artifact in artifacts:
            assert verify_export_artifact_integrity(db, artifact).is_file()

        for target, version, actor, suffix in (
            ("evidence_review", 1, seeded.analyst, "evidence"),
            ("ready_for_internal_review", 2, seeded.analyst, "ready"),
            ("internal_packet_approved", 3, seeded.approver, "approved"),
        ):
            transition_case(
                db,
                principal=actor,
                case_id=seeded.case_id,
                payload=TransitionRequest(
                    target=target,
                    expected_version=version,
                    reason=f"Prepare PostgreSQL reopen race {suffix}",
                ),
                correlation_id=f"pg-reopen-prepare-{suffix}",
            )
            db.commit()
        approved_artifact = create_export(
            db,
            principal=seeded.approver,
            case_id=seeded.case_id,
            kind=ExportKind.INTERNAL_REVIEW_PDF,
            calculation_id=seeded.calculation_id,
            correlation_id="pg-reopen-initial-approved",
        )
        db.commit()
        assert approved_artifact.superseded is False

    reopen_barrier = Barrier(2)

    def racing_export() -> tuple[str, str | int]:
        with postgres_harness.sessions() as db:
            reopen_barrier.wait(timeout=10)
            try:
                artifact = create_export(
                    db,
                    principal=seeded.approver,
                    case_id=seeded.case_id,
                    kind=ExportKind.INTERNAL_REVIEW_PDF,
                    calculation_id=seeded.calculation_id,
                    correlation_id="pg-reopen-racing-export",
                )
                db.commit()
                return ("created", artifact.id)
            except HTTPException as exc:
                db.rollback()
                return ("rejected", exc.status_code)

    def reopen() -> str:
        with postgres_harness.sessions() as db:
            reopen_barrier.wait(timeout=10)
            case = transition_case(
                db,
                principal=seeded.approver,
                case_id=seeded.case_id,
                payload=TransitionRequest(
                    target="evidence_review",
                    expected_version=4,
                    reason="Concurrent new evidence requires a fresh revision",
                ),
                correlation_id="pg-reopen-racing-transition",
            )
            db.commit()
            return case.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        export_future = pool.submit(racing_export)
        reopen_future = pool.submit(reopen)
        export_result = export_future.result(timeout=20)
        assert reopen_future.result(timeout=20) == seeded.case_id
    assert export_result[0] in {"created", "rejected"}
    if export_result[0] == "rejected":
        assert export_result[1] in {404, 409, 422}

    with postgres_harness.sessions() as db:
        case = db.get(RescueCase, seeded.case_id)
        assert case is not None and case.current_revision_number == 2
        old_artifacts = list(
            db.scalars(
                select(ExportArtifact).where(ExportArtifact.case_revision_id == seeded.revision_id)
            )
        )
        assert old_artifacts and all(artifact.superseded for artifact in old_artifacts)
        assert not db.scalar(
            select(ExportArtifact).where(
                ExportArtifact.case_revision_id != seeded.revision_id,
                ExportArtifact.superseded.is_(False),
            )
        )


def test_postgres_download_lock_prevents_mid_response_retirement(
    postgres_harness: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_case(postgres_harness, include_fee=True, include_calculation=True)
    assert seeded.calculation_id is not None
    with postgres_harness.sessions() as db:
        artifact = create_export(
            db,
            principal=seeded.analyst,
            case_id=seeded.case_id,
            kind=ExportKind.MACHINE_READABLE_JSON,
            calculation_id=seeded.calculation_id,
            correlation_id="pg-download-lock-artifact",
        )
        artifact_id = artifact.id
        db.commit()

    freshness_checked = Event()
    mutation_started = Event()
    original_build = export_service.build_export_packet

    def synchronized_build(*args: Any, **kwargs: Any) -> Any:
        prepared = original_build(*args, **kwargs)
        freshness_checked.set()
        assert mutation_started.wait(timeout=10)
        return prepared

    monkeypatch.setattr(export_service, "build_export_packet", synchronized_build)

    def download() -> float:
        with postgres_harness.sessions() as db:
            prepared = prepare_download(
                db,
                principal=seeded.analyst,
                case_id=seeded.case_id,
                artifact_id=artifact_id,
                correlation_id="pg-download-lock-download",
            )
            assert prepared.path.is_file()
            return time.monotonic()

    def mutate() -> float:
        assert freshness_checked.wait(timeout=10)
        mutation_started.set()
        with postgres_harness.sessions() as db:
            add_fee(
                db,
                principal=seeded.analyst,
                case_id=seeded.case_id,
                payload=_unreviewed_fee(1),
                correlation_id="pg-download-lock-mutation",
            )
            db.commit()
            return time.monotonic()

    with ThreadPoolExecutor(max_workers=2) as pool:
        download_future = pool.submit(download)
        mutation_future = pool.submit(mutate)
        download_finished = download_future.result(timeout=20)
        mutation_finished = mutation_future.result(timeout=20)
    assert download_finished <= mutation_finished

    with postgres_harness.sessions() as db:
        retired = db.get(ExportArtifact, artifact_id)
        assert retired is not None and retired.superseded is True
        with pytest.raises(HTTPException) as captured:
            prepare_download(
                db,
                principal=seeded.analyst,
                case_id=seeded.case_id,
                artifact_id=artifact_id,
                correlation_id="pg-download-lock-retired",
            )
        assert captured.value.status_code == 409
        assert db.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "export.superseded",
                AuditEvent.object_id == artifact_id,
            )
        )


def test_postgres_workbench_snapshot_serializes_with_same_case_mutation(
    postgres_harness: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_case(postgres_harness, include_fee=True, include_calculation=True)
    assert seeded.calculation_id is not None
    case_locked = Event()
    mutation_started = Event()
    original_list_documents = workbench_service.list_documents

    def synchronized_list_documents(*args: Any, **kwargs: Any) -> Any:
        case_locked.set()
        assert mutation_started.wait(timeout=10)
        return original_list_documents(*args, **kwargs)

    monkeypatch.setattr(
        workbench_service,
        "list_documents",
        synchronized_list_documents,
    )

    def snapshot() -> tuple[Any, float]:
        with postgres_harness.sessions() as db:
            result = build_workbench_snapshot(
                db,
                principal=seeded.analyst,
                case_id=seeded.case_id,
            )
            return result, time.monotonic()

    def mutate() -> float:
        assert case_locked.wait(timeout=10)
        mutation_started.set()
        with postgres_harness.sessions() as db:
            add_fee(
                db,
                principal=seeded.analyst,
                case_id=seeded.case_id,
                payload=_unreviewed_fee(1),
                correlation_id="pg-workbench-snapshot-mutation",
            )
            db.commit()
            return time.monotonic()

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot_future = pool.submit(snapshot)
        mutation_future = pool.submit(mutate)
        captured, snapshot_finished = snapshot_future.result(timeout=20)
        mutation_finished = mutation_future.result(timeout=20)

    assert snapshot_finished <= mutation_finished
    assert captured.case_detail.current_revision_number == 1
    assert len(captured.fees) == 1
    assert captured.calculation is not None
    assert captured.calculation.id == seeded.calculation_id
    assert captured.readiness.ready_for_internal_review is True
    assert captured.readiness.reproducible_calculation_exists is True
    assert captured.readiness.open_blocking_findings == 0

    with postgres_harness.sessions() as db:
        post_mutation = build_workbench_snapshot(
            db,
            principal=seeded.analyst,
            case_id=seeded.case_id,
        )
        assert len(post_mutation.fees) == 2
        assert post_mutation.readiness.ready_for_internal_review is False
        assert post_mutation.readiness.reproducible_calculation_exists is False
        assert post_mutation.readiness.open_blocking_findings > 0


def test_postgres_processing_failure_replays_422_and_recovers_with_new_key(
    postgres_harness: PostgresHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with postgres_harness.sessions() as db:
        suffix = uuid.uuid4().hex
        organization = Organization(name=f"PostgreSQL failure replay {suffix}")
        user = User(
            email=f"failure-{suffix}@example.com",
            password_hash=hash_password("PostgresFailure!2026"),
            display_name="PostgreSQL Failure Analyst",
        )
        db.add_all([organization, user])
        db.flush()
        membership = Membership(
            organization_id=organization.id,
            user_id=user.id,
            role=Role.ANALYST,
        )
        db.add(membership)
        db.commit()
        token, _ = create_access_token(user=user, membership=membership)
        organization_id = organization.id

    def override_db() -> Iterator[Session]:
        with postgres_harness.sessions() as db:
            try:
                yield db
            except BaseException:
                db.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    original_persist = documents_service._persist_extraction_result
    persistence_attempts = 0

    def fail_after_partial_persist(*args: Any, **kwargs: Any) -> Any:
        nonlocal persistence_attempts
        persistence_attempts += 1
        original_persist(*args, **kwargs)
        session = args[0]
        assert isinstance(session, Session)
        session.flush()
        raise RuntimeError("injected PostgreSQL persistence failure")

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            created = client.post(
                "/v1/cases",
                headers={**headers, "Idempotency-Key": "pg-failure-case"},
                json={
                    "display_name": "PostgreSQL processing failure",
                    "applicant_company": "Synthetic Applicant",
                    "erp_provider": "Synthetic ERP",
                },
            )
            assert created.status_code == 201, created.text
            uploaded = client.post(
                f"/v1/cases/{created.json()['id']}/documents",
                headers={**headers, "Idempotency-Key": "pg-failure-upload"},
                data={"source_type": "synthetic"},
                files={
                    "file": (
                        "clean_standard.pdf",
                        (FIXTURES / "clean_standard.pdf").read_bytes(),
                        "application/pdf",
                    )
                },
            )
            assert uploaded.status_code == 201, uploaded.text
            document_id = str(uploaded.json()["id"])
            monkeypatch.setattr(
                documents_service,
                "_persist_extraction_result",
                fail_after_partial_persist,
            )
            failure_headers = {
                **headers,
                "Idempotency-Key": "pg-failure-process",
            }
            first = client.post(
                f"/v1/documents/{document_id}/process",
                headers=failure_headers,
            )
            replay = client.post(
                f"/v1/documents/{document_id}/process",
                headers=failure_headers,
            )
            assert first.status_code == replay.status_code == 422
            assert replay.json() == first.json()
            assert persistence_attempts == 1

            with postgres_harness.sessions() as db:
                document = db.get(ContractDocument, document_id)
                assert document is not None and document.processing_status.value == "failed"
                assert db.query(DocumentPage).filter_by(document_id=document_id).count() == 0
                assert db.query(ExtractionRun).filter_by(document_id=document_id).count() == 0
                assert db.query(EvidenceSpan).count() == 0
                assert db.query(ContractAssertion).count() == 0
                reservation = db.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.organization_id == organization_id,
                        IdempotencyRecord.endpoint == f"POST /v1/documents/{document_id}/process",
                        IdempotencyRecord.key == "pg-failure-process",
                    )
                )
                assert reservation is not None and reservation.response_status == 422
                assert (
                    db.query(AuditEvent)
                    .filter_by(action="document.processing_failed", object_id=document_id)
                    .count()
                    == 1
                )

            monkeypatch.setattr(
                documents_service,
                "_persist_extraction_result",
                original_persist,
            )
            recovered = client.post(
                f"/v1/documents/{document_id}/process",
                headers={**headers, "Idempotency-Key": "pg-failure-recovery"},
            )
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["processing_status"] == "processed"
    finally:
        app.dependency_overrides.pop(get_db, None)
