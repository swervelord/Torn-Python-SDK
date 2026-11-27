"""Common routing behavior for generated resource facades."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from ._contracts import R, Selection
from .errors import DecodeError, ValidationError
from .planner import BatchResult, RequestPlan, SelectionInput, TargetValue, Version, plan_selections
from .runtime import JsonValue, Query, QueryValue, RequestOptions

if TYPE_CHECKING:
    from .client import AsyncTornClient, TornClient


def merge_filters(query: Query | None, filters: Mapping[str, QueryValue]) -> Query:
    """Apply explicit typed filters while preserving passthrough query repetitions."""
    if query is None:
        return filters
    if isinstance(query, Mapping):
        return {**query, **filters}
    return (*((name, value) for name, value in query if name not in filters), *filters.items())


class _Resource:
    resource: ClassVar[str]

    def __init__(self, *, id: TargetValue | None = None) -> None:
        self.id = id

    def plan(
        self,
        *selections: SelectionInput,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        force_direct: bool = False,
    ) -> RequestPlan:
        return plan_selections(
            self.resource,
            selections,
            id=self.id,
            query=query,
            path_args=path_args,
            version=version,
            force_direct=force_direct,
        )

    def _check_selection(self, selection: Selection[object]) -> None:
        if selection.resource != self.resource:
            raise ValidationError(f"Selection does not belong to resource {self.resource}")

    @staticmethod
    def _raw_value(batch: BatchResult, selection: Selection[object]) -> dict[str, JsonValue] | str:
        value = batch.raise_for_errors().results[selection.name].raw
        if isinstance(value, (dict, str)):
            return value
        raise DecodeError(selection.qualified_name, "$", "JSON object or CSV text")


class Resource(_Resource):
    """Use generated methods or send arbitrary selections for one resource."""

    def __init__(self, client: TornClient, *, id: TargetValue | None = None) -> None:
        super().__init__(id=id)
        self._client = client

    def batch(
        self,
        *selections: SelectionInput,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        force_direct: bool = False,
        options: RequestOptions | None = None,
        strict: bool | None = None,
        typed: bool = True,
    ) -> BatchResult:
        return self._client.batch(
            self.resource,
            selections,
            id=self.id,
            query=query,
            path_args=path_args,
            version=version,
            force_direct=force_direct,
            options=options,
            strict=strict,
            typed=typed,
        )

    def select(
        self,
        selection: Selection[R],
        *,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
        strict: bool | None = None,
    ) -> R:
        self._check_selection(selection)
        return self.batch(
            selection,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
            strict=strict,
        ).get(selection)

    def raw_selection(
        self,
        selection: str,
        *,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
    ) -> dict[str, JsonValue]:
        return self.raw_selections(
            selection,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
        )

    def _raw_selected(
        self,
        selection: Selection[object],
        *,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
    ) -> dict[str, JsonValue] | str:
        self._check_selection(selection)
        batch = self.batch(
            selection,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
            typed=False,
        )
        return self._raw_value(batch, selection)

    def raw_selections(
        self,
        *selections: str,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
    ) -> dict[str, JsonValue]:
        return (
            self.batch(
                *selections,
                query=query,
                path_args=path_args,
                version=version,
                options=options,
                typed=False,
            )
            .raise_for_errors()
            .data
        )


class AsyncResource(_Resource):
    """Asynchronous resource selection and batch methods."""

    def __init__(self, client: AsyncTornClient, *, id: TargetValue | None = None) -> None:
        super().__init__(id=id)
        self._client = client

    async def batch(
        self,
        *selections: SelectionInput,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        force_direct: bool = False,
        options: RequestOptions | None = None,
        strict: bool | None = None,
        typed: bool = True,
    ) -> BatchResult:
        return await self._client.batch(
            self.resource,
            selections,
            id=self.id,
            query=query,
            path_args=path_args,
            version=version,
            force_direct=force_direct,
            options=options,
            strict=strict,
            typed=typed,
        )

    async def select(
        self,
        selection: Selection[R],
        *,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
        strict: bool | None = None,
    ) -> R:
        self._check_selection(selection)
        result = await self.batch(
            selection,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
            strict=strict,
        )
        return result.get(selection)

    async def raw_selection(
        self,
        selection: str,
        *,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
    ) -> dict[str, JsonValue]:
        return await self.raw_selections(
            selection,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
        )

    async def _raw_selected(
        self,
        selection: Selection[object],
        *,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
    ) -> dict[str, JsonValue] | str:
        self._check_selection(selection)
        batch = await self.batch(
            selection,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
            typed=False,
        )
        return self._raw_value(batch, selection)

    async def raw_selections(
        self,
        *selections: str,
        query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None,
        version: Version = "auto",
        options: RequestOptions | None = None,
    ) -> dict[str, JsonValue]:
        result = await self.batch(
            *selections,
            query=query,
            path_args=path_args,
            version=version,
            options=options,
            typed=False,
        )
        return result.raise_for_errors().data
