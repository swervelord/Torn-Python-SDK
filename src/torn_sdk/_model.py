"""Wire models and forward-compatible enum decoding."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from enum import Enum
from functools import lru_cache
from typing import ClassVar, TypeVar, cast

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler, TypeAdapter
from pydantic import ValidationInfo, model_validator
from pydantic_core import PydanticCustomError, core_schema


class _OpenEnum:
    """Behavior shared by generated string and integer enums."""

    @property
    def is_known(self) -> bool:
        return isinstance(self, Enum) and bool(self.name)

    @classmethod
    def known_values(cls) -> tuple[str | int, ...]:
        if not issubclass(cls, Enum):
            raise TypeError("Open enum must inherit Enum")
        return tuple(member.value for member in cls)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.with_info_after_validator_function(
            cls._validate,
            handler(source_type),
        )

    @classmethod
    def _validate(cls, member: _OpenEnum, info: ValidationInfo) -> _OpenEnum:
        if not member.is_known and isinstance(info.context, Mapping):
            if info.context.get("strict_contract"):
                raise PydanticCustomError(
                    "unknown_enum",
                    "Unknown enum value (expected one of {values})",
                    {"values": ", ".join(map(str, cls.known_values()))},
                )
        return member


class OpenEnum(_OpenEnum, str, Enum):
    """An open string enum. Unknown values retain their original wire value."""

    @classmethod
    def _missing_(cls, value: object) -> OpenEnum | None:
        if not isinstance(value, str):
            return None
        member = str.__new__(cls, value)
        member._name_ = ""
        member._value_ = value
        return member

    def __str__(self) -> str:
        return str.__str__(self)


class OpenIntEnum(_OpenEnum, int, Enum):
    """An open integer enum."""

    @classmethod
    def _missing_(cls, value: object) -> OpenIntEnum | None:
        if type(value) is not int:
            return None
        member = int.__new__(cls, value)
        member._name_ = ""
        member._value_ = value
        return member


class WireModel(BaseModel):
    """Base for typed response models. Unknown properties travel with the model."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, loc_by_alias=True)
    __relaxed_fields__: ClassVar[tuple[str, ...]] = ()
    __accept_extra__: ClassVar[bool] = False

    @model_validator(mode="before")
    @classmethod
    def _check_strict(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, Mapping) or not isinstance(info.context, Mapping):
            return value
        if not info.context.get("strict_contract"):
            return value
        from pydantic import AliasChoices
        from pydantic_core import InitErrorDetails, ValidationError

        accepted: set[str] = set()
        missing: list[InitErrorDetails] = []
        for name, field in cls.model_fields.items():
            key = field.alias or name
            aliases = [name, key]
            if isinstance(field.validation_alias, AliasChoices):
                aliases.extend(a for a in field.validation_alias.choices if isinstance(a, str))
            accepted.update(aliases)
            if key in cls.__relaxed_fields__ and not any(alias in value for alias in aliases):
                missing.append(InitErrorDetails(type="missing", loc=(key,), input=value))
        if not cls.__accept_extra__:
            missing.extend(
                InitErrorDetails(type="extra_forbidden", loc=(str(key),), input=item)
                for key, item in value.items()
                if key not in accepted
            )
        if missing:
            raise ValidationError.from_exception_data(cls.__name__, missing)
        return value


class RequestParams(BaseModel):
    """Typed operation inputs. Additional query parameters can be passed separately."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


T = TypeVar("T")


@lru_cache(maxsize=None)
def _adapter(model: Hashable) -> TypeAdapter[object]:
    return TypeAdapter(model)  # type: ignore[arg-type]


def decode_payload(model: type[T], payload: object, *, strict: bool = False) -> T:
    """Decode JSON payload data into a generated model or type alias."""
    return cast(
        "T",
        _adapter(cast(Hashable, model)).validate_python(payload, context={"strict_contract": strict}),
    )
