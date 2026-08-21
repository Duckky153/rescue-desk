import pytest

from rescue_desk.domain.evidence import EvidenceValidationError, verify_evidence_span


def test_verifies_exact_evidence_span() -> None:
    text = "Subscription fees are $120,000 per year."
    start = text.index("$120,000")
    result = verify_evidence_span(
        page_text=text,
        quote="$120,000",
        char_start=start,
        char_end=start + len("$120,000"),
    )
    assert result.quote == "$120,000"
    assert len(result.quote_sha256) == 64


@pytest.mark.parametrize(
    ("quote", "start", "end"),
    [("$120,001", 22, 30), ("", 0, 0), ("outside", -1, 6), ("outside", 0, 999)],
)
def test_rejects_invalid_evidence_spans(quote: str, start: int, end: int) -> None:
    with pytest.raises(EvidenceValidationError):
        verify_evidence_span(
            page_text="Subscription fees are $120,000 per year.",
            quote=quote,
            char_start=start,
            char_end=end,
        )
