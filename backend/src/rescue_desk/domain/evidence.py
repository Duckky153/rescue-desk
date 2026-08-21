from dataclasses import dataclass

from rescue_desk.domain.hashing import sha256_text


class EvidenceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedEvidence:
    quote: str
    char_start: int
    char_end: int
    quote_sha256: str


def verify_evidence_span(
    *, page_text: str, quote: str, char_start: int, char_end: int
) -> VerifiedEvidence:
    if char_start < 0 or char_end <= char_start or char_end > len(page_text):
        raise EvidenceValidationError("Evidence offsets are outside the page text")
    exact_text = page_text[char_start:char_end]
    if exact_text != quote:
        raise EvidenceValidationError("Evidence quote does not match the cited page span")
    if not quote.strip():
        raise EvidenceValidationError("Evidence quote cannot be blank")
    return VerifiedEvidence(
        quote=quote,
        char_start=char_start,
        char_end=char_end,
        quote_sha256=sha256_text(quote),
    )


def verify_persisted_evidence_span(
    *,
    page_text: str,
    page_text_sha256: str,
    quote: str,
    quote_sha256: str,
    char_start: int,
    char_end: int,
) -> VerifiedEvidence:
    """Verify persisted page and quote hashes plus exact offset identity."""

    if sha256_text(page_text) != page_text_sha256:
        raise EvidenceValidationError("Persisted page text failed its SHA-256 integrity check")
    verified = verify_evidence_span(
        page_text=page_text,
        quote=quote,
        char_start=char_start,
        char_end=char_end,
    )
    if verified.quote_sha256 != quote_sha256:
        raise EvidenceValidationError("Persisted evidence quote failed its SHA-256 integrity check")
    return verified
