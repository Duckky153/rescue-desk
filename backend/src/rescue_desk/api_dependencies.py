import uuid
from typing import Annotated

from fastapi import Header


def correlation_id(
    value: Annotated[
        str | None,
        Header(
            alias="X-Correlation-ID",
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
    ] = None,
) -> str:
    return value or str(uuid.uuid4())


def idempotency_key(
    value: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=160)],
) -> str:
    return value
