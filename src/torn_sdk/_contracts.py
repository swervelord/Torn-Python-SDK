"""Typed metadata shared by generated endpoint and selection declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from ._model import RequestParams
from .errors import AccessLevel

P = TypeVar("P", bound=RequestParams, covariant=True)
R = TypeVar("R", covariant=True)


@dataclass(frozen=True)
class Parameter:
    """A named path or query parameter on an exact operation."""

    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool = False
    choices: tuple[str | int | bool, ...] = ()
    style: str | None = None
    explode: bool = False


@dataclass(frozen=True)
class Operation(Generic[P, R]):
    """An OpenAPI operation and its exact parameter and response contracts."""

    id: str
    path: str
    params: type[P]
    response: type[R]
    parameters: tuple[Parameter, ...]
    access: AccessLevel
    cache_ttl: int | None
    paginated: bool
    media_type: str
    summary: str
    stability: str = "Unknown"
    deprecated: bool = False
    method: Literal["GET"] = "GET"
    item_field: tuple[str, ...] | None = None
    item_type: object | None = None


@dataclass(frozen=True)
class Endpoint:
    """A dedicated endpoint for a selection."""

    id: str
    path: str
    parameters: tuple[str, ...]
    required: tuple[str, ...]
    schema: str | None


@dataclass(frozen=True)
class Selection(Generic[R]):
    """A typed selection token which can be used by a resource or batch."""

    resource: str
    name: str
    response: type[R]
    legacy_response: object | None
    access: AccessLevel
    endpoints: tuple[Endpoint, ...]
    generic: bool
    standalone: bool
    legacy: bool
    csv: bool

    @property
    def qualified_name(self) -> str:
        return f"{self.resource}/{self.name}"
