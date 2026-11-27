"""Typed Torn errors and error classification."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from ._error_codes import HTTP_RETRY_STATUSES, TornErrorCode as TornErrorCode

AccessLevel = Literal["public", "minimal", "limited", "full", "custom"]


class TornError(Exception):
    """Base class for SDK request failures."""

    @property
    def code(self) -> TornErrorCode | None:
        """The API code, if this failure contains an API error."""
        return code(self)


class ApiError(TornError):
    """A Torn API error envelope."""

    def __init__(
        self,
        code: int | TornErrorCode,
        message: str,
        *,
        key_hint: str = "",
        operation: str = "",
    ) -> None:
        self._code = TornErrorCode(code)
        self.message = message
        self.key_hint = key_hint
        self.operation = operation
        super().__init__(f"Torn API error {int(self._code)}: {message}")

    @property
    def code(self) -> TornErrorCode:
        return self._code


class HttpError(TornError):
    """A non-success HTTP response without a Torn error envelope."""

    def __init__(self, status: int, *, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"Torn HTTP status {status}")

    @property
    def status_code(self) -> int:
        return self.status


class TransportError(TornError):
    """The HTTP transport failed. Its cause is stripped of request details."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        self.__cause__ = cause
        super().__init__(f"Torn transport error: {cause}")


class DecodeError(TornError):
    """A response could not be decoded as the advertised model."""

    def __init__(
        self,
        operation: str,
        path: str,
        expected: str,
        value: str = "",
        raw: bytes = b"",
        *,
        strict: bool = False,
    ) -> None:
        self.operation = operation
        self.path = path
        self.expected = expected
        self.value = value[:160]
        self.strict = strict
        self._raw = raw
        description = f"Torn decode {operation} at {path}: expected {expected}"
        if self.value:
            description += f", got {self.value}"
        super().__init__(description)

    @property
    def raw(self) -> bytes:
        """The complete response bytes, which may contain sensitive data."""
        return self._raw


class ValidationError(TornError):
    """Request parameters failed validation before a send."""


class AccessError(ValidationError):
    """Known key access does not satisfy the operation's access requirement."""

    def __init__(
        self, operation: str, required: AccessLevel, have: tuple[AccessLevel, ...] = ()
    ) -> None:
        self.operation = operation
        self.required = required
        self.have = have
        super().__init__(f"Torn {operation} requires a {required} access key")


class NoUsableKeysError(TornError):
    """The pool has no usable keys for the requested operation."""

    def __init__(self, last: ApiError | None = None) -> None:
        self.last = last
        message = "Torn has no usable API keys"
        if last is not None:
            self.__cause__ = last
            message += f"; last error: {last}"
        super().__init__(message)


class CancelledError(TornError):
    """A request was cancelled through its options."""


class TimeoutError(TornError):
    """The request deadline elapsed."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        cause: Exception | None = None,
    ) -> None:
        self.retryable = retryable
        self.__cause__ = cause
        super().__init__(message)


def _causes(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__


def code(error: BaseException) -> TornErrorCode | None:
    """Return the Torn code carried by the exception chain."""
    return next((cause.code for cause in _causes(error) if isinstance(cause, ApiError)), None)


def is_retryable(error: BaseException) -> bool:
    value = code(error)
    if value is not None:
        return value.action in {"retry_backoff", "wait_rate_window", "rotate_route"}
    return any(
        isinstance(cause, TransportError)
        or isinstance(cause, HttpError)
        and cause.status in HTTP_RETRY_STATUSES
        or isinstance(cause, TimeoutError)
        and cause.retryable
        for cause in _causes(error)
    )


def is_key_error(error: BaseException) -> bool:
    value = code(error)
    return any(isinstance(cause, NoUsableKeysError) for cause in _causes(error)) or (
        value is not None and value.category == "key"
    )


def is_rate_limit(error: BaseException) -> bool:
    value = code(error)
    return value in {TornErrorCode.TOO_MANY_REQUESTS, TornErrorCode.IP_BLOCKED} or any(
        isinstance(cause, HttpError) and cause.status == 429 for cause in _causes(error)
    )


def is_access_error(error: BaseException) -> bool:
    return code(error) in {
        TornErrorCode.ACCESS_LEVEL_TOO_LOW,
        TornErrorCode.INCORRECT_ID_ENTITY_RELATION,
    } or any(isinstance(cause, AccessError) for cause in _causes(error))


def is_transient(error: BaseException) -> bool:
    value = code(error)
    return value.action == "retry_backoff" if value is not None else is_retryable(error)


def is_decode_error(error: BaseException) -> bool:
    return any(isinstance(cause, DecodeError) for cause in _causes(error))


APIError = ApiError
HTTPError = HttpError
