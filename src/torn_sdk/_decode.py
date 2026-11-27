"""Decode SDK models with a redacted failure location and original response bytes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

from pydantic import ValidationError as ModelValidationError

from ._model import decode_payload
from .errors import DecodeError

T = TypeVar("T")


def decode_response(
    model: type[T],
    payload: object,
    *,
    operation: str,
    strict: bool = False,
    raw: bytes | None = None,
    redact: Callable[[str], str] | None = None,
) -> T:
    """Decode one response, preserving the raw body on failure."""
    try:
        return decode_payload(model, payload, strict=strict)
    except ModelValidationError as error:
        issue = error.errors(include_url=False)[0]
        parts = (
            f"[{part}]" if isinstance(part, int) else
            f".{part}" if part.isidentifier() else f"[{json.dumps(part)}]"
            for part in issue["loc"]
        )
        path = "$" + "".join(parts)
        expected = issue["msg"]
        value = ""
        if redact is not None:
            operation, path, expected = redact(operation), redact(path), redact(expected)
            value = redact(repr(issue.get("input"))[:4096])
        failure = DecodeError(
            operation, path, expected, value,
            raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            strict=strict,
        )
    raise failure
