"""Open enum behavior for the generated Torn error code catalog."""

from __future__ import annotations

from enum import IntEnum
from typing import TypeVar

_Code = TypeVar("_Code", bound="_ErrorCodeBehavior")


class _ErrorCodeBehavior(IntEnum):
    @classmethod
    def _missing_(cls: type[_Code], value: object) -> _Code | None:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        unknown = int.__new__(cls, value)
        unknown._value_ = value
        unknown._name_ = f"UNKNOWN_{value}"
        return unknown

    @property
    def _policy(self) -> tuple[str, str, str, str, str, str, bool]:
        from ._error_codes import ERROR_POLICY

        return ERROR_POLICY.get(
            int(self), (f"Unknown{int(self)}", "unknown", "", "request", "fail", "none", False)
        )

    @property
    def is_known(self) -> bool:
        from ._error_codes import ERROR_POLICY

        return int(self) in ERROR_POLICY

    @property
    def documented_name(self) -> str:
        return self._policy[0]

    @property
    def label(self) -> str:
        return self._policy[1]

    @property
    def message(self) -> str:
        return self._policy[2]

    @property
    def category(self) -> str:
        return self._policy[3]

    @property
    def action(self) -> str:
        return self._policy[4]

    @property
    def disable_key(self) -> str:
        return self._policy[5]

    @property
    def retryable(self) -> bool:
        return self._policy[6]

    def __str__(self) -> str:
        return f"{self.label}({int(self)})"
