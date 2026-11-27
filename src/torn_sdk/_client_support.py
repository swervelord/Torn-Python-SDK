"""Build and decode requests shared by the synchronous and asynchronous clients."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, TypeVar, cast
from urllib.parse import quote

from pydantic import BaseModel, RootModel
from pydantic import ValidationError as ModelValidationError

from ._contracts import Operation, P, R
from ._decode import decode_response
from .errors import ValidationError
from .runtime import AsyncRuntime, Query, QueryValue, Request, RequestOptions, Runtime

T = TypeVar("T")
RawVersion = Literal["v1", "v2"]


def _query_items(query: Query | None) -> Sequence[tuple[str, QueryValue]]:
    if query is None:
        return ()
    return tuple(query.items()) if isinstance(query, Mapping) else query


def _wire_text(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _array(value: object) -> bool:
    return isinstance(value, (list, tuple))


def operation_request(
    operation: Operation[P, R], params: P | None, query: Query | None
) -> Request:
    try:
        validated = operation.params.model_validate({}) if params is None else params
    except ModelValidationError:
        raise ValidationError(f"Missing required parameters for {operation.id}") from None
    if not isinstance(validated, operation.params):
        raise ValidationError(f"Expected {operation.params.__name__} for {operation.id}")
    values = cast(
        "Mapping[str, object]",
        validated.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude_none=True),
    )
    path = operation.path
    encoded: list[tuple[str, QueryValue]] = []
    extras = _query_items(query)
    for parameter in operation.parameters:
        value = values.get(parameter.name)
        if value is None:
            continue
        if parameter.location == "path":
            if _array(value):
                text = ",".join(
                    quote(_wire_text(part), safe="") for part in cast(Sequence[object], value)
                )
            else:
                text = quote(_wire_text(value), safe="")
            path = path.replace("{" + parameter.name + "}", text)
        if parameter.location == "query" and not any(name == parameter.name for name, _ in extras):
            if _array(value):
                parts = [_wire_text(part) for part in cast(Sequence[object], value)]
                if parameter.explode:
                    encoded.extend((parameter.name, part) for part in parts)
                else:
                    encoded.append((parameter.name, ",".join(parts)))
            else:
                encoded.append((parameter.name, _wire_text(value)))
    if "{" in path or "}" in path:
        raise ValidationError(f"Missing required path parameters for {operation.id}")
    encoded.extend(extras)
    if not path.startswith("/v2/"):
        path = f"/v2{path}"
    return Request(
        path, encoded, operation.id, operation.access, operation.cache_ttl,
        expect_json=operation.media_type == "application/json",
    )


def raw_request(path: str, query: Query | None, version: RawVersion) -> Request:
    if not path.startswith("/") or path.startswith("//"):
        raise ValidationError("API paths must start with a single slash")
    if version == "v2" and path != "/v2" and not path.startswith("/v2/"):
        path = f"/v2{path}"
    return Request(path, query)


def decode(
    runtime: Runtime | AsyncRuntime,
    model: type[T],
    payload: object,
    operation: str,
    options: RequestOptions | None,
    strict: bool,
    *,
    raw: bytes | None = None,
    request: Request | None = None,
) -> T:
    return decode_response(
        model, payload, operation=operation, strict=strict, raw=raw,
        redact=lambda value: runtime.redact(value, options, request),
    )


def items_at(page: object, path: tuple[str, ...] | None) -> Sequence[object]:
    for segment in path or ():
        if isinstance(page, RootModel):
            page = page.root
        if isinstance(page, Mapping):
            page = page.get(segment)
        elif isinstance(page, BaseModel):
            field_name = next(
                (name for name, field in type(page).model_fields.items()
                 if (field.alias or name) == segment),
                segment,
            )
            page = getattr(page, field_name, None)
        else:
            return ()
    if isinstance(page, RootModel):
        page = page.root
    if isinstance(page, Mapping):
        return tuple(page.values())
    if isinstance(page, (list, tuple)):
        return page
    return ()
