from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.auth import Principal, hash_password
from rescue_desk.database import SessionLocal
from rescue_desk.demo import (
    DEMO_COMPLETED_CASE_NAME,
    DEMO_GUIDED_CASE_NAME,
    DEMO_PENDING_SEMANTIC_KEY,
)
from rescue_desk.models import (
    AssertionReviewState,
    CalculationRun,
    CaseStatus,
    ContractAssertion,
    ContractDocument,
    ExportArtifact,
    ExportKind,
    FeeCategory,
    FeeObligation,
    Membership,
    Organization,
    ProcessingStatus,
    RescueCase,
    ReviewDecisionType,
    Role,
    User,
)
from rescue_desk.schemas import AssertionReviewRequest, CaseCreate, FeeCreate, TransitionRequest
from rescue_desk.services.calculations import run_calculation
from rescue_desk.services.cases import create_case, transition_case
from rescue_desk.services.documents import process_document, upload_document
from rescue_desk.services.exports import create_export
from rescue_desk.services.readiness import compute_readiness
from rescue_desk.services.review import add_fee, review_assertion

DEMO_PASSWORD = "DemoPassword!2026"
DEMO_ORGANIZATION = "RescueDesk Demonstration"


@dataclass(frozen=True)
class DemoSeedResult:
    organization_id: str
    case_id: str
    completed_case_id: str
    analyst_email: str
    approver_email: str
    auditor_email: str
    created: bool


@dataclass(frozen=True)
class _SeededCase:
    case: RescueCase
    calculation: CalculationRun
    created: bool


def _upsert_user(
    db: Session,
    *,
    organization: Organization,
    email: str,
    display_name: str,
    role: Role,
) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            display_name=display_name,
            password_hash=hash_password(DEMO_PASSWORD),
        )
        db.add(user)
        db.flush()
    membership = db.scalar(
        select(Membership).where(
            Membership.organization_id == organization.id,
            Membership.user_id == user.id,
        )
    )
    if membership is None:
        db.add(
            Membership(
                organization_id=organization.id,
                user_id=user.id,
                role=role,
            )
        )
    elif membership.role != role:
        membership.role = role
    db.flush()
    return user


def _principal(user: User, organization: Organization, role: Role) -> Principal:
    return Principal(
        user_id=user.id,
        organization_id=organization.id,
        role=role,
        email=user.email,
        display_name=user.display_name,
    )


def _case_by_name(
    db: Session, *, organization: Organization, display_name: str
) -> RescueCase | None:
    return db.scalar(
        select(RescueCase).where(
            RescueCase.organization_id == organization.id,
            RescueCase.display_name == display_name,
        )
    )


def _ensure_case_foundation(
    db: Session,
    *,
    organization: Organization,
    assigned_analyst: User,
    assigned_approver: User,
    seed_analyst_principal: Principal,
    fixture_path: Path,
    display_name: str,
    correlation_prefix: str,
    pending_semantic_key: str | None,
) -> _SeededCase:
    case = _case_by_name(db, organization=organization, display_name=display_name)
    created = case is None
    if case is None:
        case = create_case(
            db,
            principal=seed_analyst_principal,
            payload=CaseCreate(
                display_name=display_name,
                applicant_company="Northstar Systems",
                erp_provider="LegacySuite ERP",
                assigned_analyst_id=assigned_analyst.id,
                assigned_approver_id=assigned_approver.id,
            ),
            correlation_id=f"{correlation_prefix}-case",
        )

    revision = max(case.revisions, key=lambda item: item.number)
    document = db.scalar(
        select(ContractDocument)
        .where(
            ContractDocument.case_revision_id == revision.id,
            ContractDocument.superseded.is_(False),
        )
        .order_by(ContractDocument.created_at, ContractDocument.id)
        .limit(1)
    )
    if document is None:
        document = upload_document(
            db,
            principal=seed_analyst_principal,
            case_id=case.id,
            original_filename="synthetic-northstar-contract.pdf",
            data=fixture_path.read_bytes(),
            source_type="synthetic",
            correlation_id=f"{correlation_prefix}-upload",
        )
        # Processing uses a savepoint. Seal intake so a transient extraction
        # rollback retains the exact bytes for the next replay-safe seed attempt.
        db.commit()
    if document.processing_status not in {
        ProcessingStatus.PROCESSED,
        ProcessingStatus.NEEDS_OCR,
    }:
        process_document(
            db,
            principal=seed_analyst_principal,
            document_id=document.id,
            correlation_id=f"{correlation_prefix}-process",
        )

    assertions = list(
        db.scalars(
            select(ContractAssertion).where(
                ContractAssertion.case_revision_id == document.case_revision_id,
                ContractAssertion.is_current.is_(True),
            )
        )
    )
    if not assertions:
        raise RuntimeError(f"The {display_name} demonstration extracted no assertions")
    by_key = {item.semantic_key: item for item in assertions}
    required_keys = {"contract_end_date", "fee.subscription", DEMO_PENDING_SEMANTIC_KEY}
    missing_keys = sorted(required_keys.difference(by_key))
    if missing_keys:
        raise RuntimeError(
            f"The {display_name} demonstration is missing assertions: {', '.join(missing_keys)}"
        )

    for assertion in sorted(assertions, key=lambda item: (item.semantic_key, item.id)):
        if assertion.semantic_key == pending_semantic_key:
            continue
        if assertion.review_state in {
            AssertionReviewState.ACCEPTED,
            AssertionReviewState.CORRECTED,
            AssertionReviewState.REJECTED,
        }:
            continue
        review_assertion(
            db,
            principal=seed_analyst_principal,
            assertion_id=assertion.id,
            payload=AssertionReviewRequest(
                decision=ReviewDecisionType.ACCEPT,
                expected_assertion_version=assertion.version,
                reason="Synthetic fixture citation verified for the local demonstration",
            ),
            correlation_id=f"{correlation_prefix}-review-{assertion.semantic_key}",
        )

    subscription = by_key["fee.subscription"]
    normalized = subscription.normalized_value
    cadence = normalized.get("cadence")
    if cadence not in {"annual", "monthly", "one_time"}:
        raise RuntimeError("Demo subscription evidence has an unsupported billing cadence")
    fee = db.scalar(
        select(FeeObligation)
        .where(
            FeeObligation.case_revision_id == document.case_revision_id,
            FeeObligation.category == FeeCategory.SUBSCRIPTION,
            FeeObligation.superseded.is_(False),
        )
        .order_by(FeeObligation.created_at, FeeObligation.id)
        .limit(1)
    )
    if fee is None:
        add_fee(
            db,
            principal=seed_analyst_principal,
            case_id=case.id,
            payload=FeeCreate(
                category=FeeCategory.SUBSCRIPTION,
                amount_minor=int(normalized["amount_minor"]),
                currency=str(normalized["currency"]),
                service_start=date(2026, 1, 15),
                service_end=date(2027, 1, 15),
                payment_status="unpaid",
                billing_cadence=cast(Literal["annual", "monthly", "one_time"], cadence),
                proration_rule="contract_daily",
                assertion_ids=[subscription.id],
                reviewed=True,
            ),
            correlation_id=f"{correlation_prefix}-fee",
        )

    calculation = db.scalar(
        select(CalculationRun)
        .where(CalculationRun.case_revision_id == document.case_revision_id)
        .order_by(CalculationRun.created_at.desc(), CalculationRun.id.desc())
        .limit(1)
    )
    readiness = compute_readiness(db, principal=seed_analyst_principal, case_id=case.id)
    if calculation is None or not bool(readiness["reproducible_calculation_exists"]):
        calculation = run_calculation(
            db,
            principal=seed_analyst_principal,
            case_id=case.id,
            as_of_date=date(2026, 8, 20),
            correlation_id=f"{correlation_prefix}-calculation",
        )

    db.flush()
    db.refresh(case)
    return _SeededCase(case=case, calculation=calculation, created=created)


def _complete_case(
    db: Session,
    *,
    seeded: _SeededCase,
    analyst_principal: Principal,
    approver_principal: Principal,
) -> RescueCase:
    case = seeded.case
    if case.status == CaseStatus.EVIDENCE_REVIEW:
        readiness = compute_readiness(db, principal=analyst_principal, case_id=case.id)
        if not bool(readiness["ready_for_internal_review"]):
            raise RuntimeError("Completed demonstration failed its internal-readiness gate")
        case = transition_case(
            db,
            principal=analyst_principal,
            case_id=case.id,
            payload=TransitionRequest(
                target=CaseStatus.READY_FOR_INTERNAL_REVIEW,
                expected_version=case.version,
                reason="Synthetic evidence and deterministic calculation are ready for review",
            ),
            correlation_id="demo-completed-ready",
        )
    if case.status == CaseStatus.READY_FOR_INTERNAL_REVIEW:
        case = transition_case(
            db,
            principal=approver_principal,
            case_id=case.id,
            payload=TransitionRequest(
                target=CaseStatus.INTERNAL_PACKET_APPROVED,
                expected_version=case.version,
                reason=("Seeded synthetic approver-role checkpoint recorded for the demonstration"),
            ),
            correlation_id="demo-completed-approved",
        )
    if case.status == CaseStatus.INTERNAL_PACKET_APPROVED:
        for kind in ExportKind:
            current_artifact = db.scalar(
                select(ExportArtifact)
                .where(
                    ExportArtifact.case_revision_id == seeded.calculation.case_revision_id,
                    ExportArtifact.kind == kind,
                    ExportArtifact.superseded.is_(False),
                )
                .limit(1)
            )
            if current_artifact is None:
                create_export(
                    db,
                    principal=analyst_principal,
                    case_id=case.id,
                    kind=kind,
                    calculation_id=seeded.calculation.id,
                    correlation_id=f"demo-completed-export-{kind.value}",
                )
        db.refresh(case)
        case = transition_case(
            db,
            principal=analyst_principal,
            case_id=case.id,
            payload=TransitionRequest(
                target=CaseStatus.EXPORTED,
                expected_version=case.version,
                reason="Approved synthetic packet formats generated and verified",
            ),
            correlation_id="demo-completed-exported",
        )
    return case


def seed_demo(db: Session, *, fixture_path: Path) -> DemoSeedResult:
    if not fixture_path.is_file():
        raise FileNotFoundError(f"Synthetic demonstration fixture not found: {fixture_path}")
    organization = db.scalar(select(Organization).where(Organization.name == DEMO_ORGANIZATION))
    if organization is None:
        organization = Organization(name=DEMO_ORGANIZATION)
        db.add(organization)
        db.flush()
    analyst = _upsert_user(
        db,
        organization=organization,
        email="analyst@rescuedesk.local",
        display_name="Avery Analyst",
        role=Role.ANALYST,
    )
    approver = _upsert_user(
        db,
        organization=organization,
        email="approver@rescuedesk.local",
        display_name="Morgan Approver",
        role=Role.APPROVER,
    )
    _upsert_user(
        db,
        organization=organization,
        email="auditor@rescuedesk.local",
        display_name="Jordan Auditor",
        role=Role.AUDITOR,
    )
    db.commit()
    seed_analyst = _upsert_user(
        db,
        organization=organization,
        email="seed-analyst@synthetic.rescuedesk.local",
        display_name="Synthetic Demo Analyst",
        role=Role.ANALYST,
    )
    seed_approver = _upsert_user(
        db,
        organization=organization,
        email="seed-approver@synthetic.rescuedesk.local",
        display_name="Synthetic Demo Approver",
        role=Role.APPROVER,
    )
    db.commit()
    seed_analyst_principal = _principal(seed_analyst, organization, Role.ANALYST)
    seed_approver_principal = _principal(seed_approver, organization, Role.APPROVER)

    # Build the completed comparison first so the guided one-action case is the
    # newest row and therefore the obvious starting point in the matter list.
    completed_seed = _ensure_case_foundation(
        db,
        organization=organization,
        assigned_analyst=seed_analyst,
        assigned_approver=seed_approver,
        seed_analyst_principal=seed_analyst_principal,
        fixture_path=fixture_path,
        display_name=DEMO_COMPLETED_CASE_NAME,
        correlation_prefix="demo-completed",
        pending_semantic_key=None,
    )
    completed_case = _complete_case(
        db,
        seeded=completed_seed,
        analyst_principal=seed_analyst_principal,
        approver_principal=seed_approver_principal,
    )
    db.commit()

    guided_seed = _ensure_case_foundation(
        db,
        organization=organization,
        assigned_analyst=analyst,
        assigned_approver=approver,
        seed_analyst_principal=seed_analyst_principal,
        fixture_path=fixture_path,
        display_name=DEMO_GUIDED_CASE_NAME,
        correlation_prefix="demo-guided",
        pending_semantic_key=DEMO_PENDING_SEMANTIC_KEY,
    )
    db.commit()
    return DemoSeedResult(
        organization_id=organization.id,
        case_id=guided_seed.case.id,
        completed_case_id=completed_case.id,
        analyst_email=analyst.email,
        approver_email=approver.email,
        auditor_email="auditor@rescuedesk.local",
        created=completed_seed.created or guided_seed.created,
    )


def _default_fixture() -> Path:
    repository_fixture = (
        Path(__file__).resolve().parents[3] / "fixtures" / "contracts" / "clean_standard.pdf"
    )
    container_fixture = Path("/app/fixtures/contracts/clean_standard.pdf")
    return repository_fixture if repository_fixture.is_file() else container_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the synthetic RescueDesk demonstration")
    parser.add_argument("--fixture", type=Path, default=_default_fixture())
    args = parser.parse_args()
    with SessionLocal() as db:
        result = seed_demo(db, fixture_path=args.fixture)
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
