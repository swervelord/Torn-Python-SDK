"""Synchronous and asynchronous Torn developer clients."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence

from typing_extensions import Self

from ._client_support import RawVersion, decode, items_at, operation_request, raw_request
from ._contracts import Operation, P, R
from ._generated_calls import AsyncExactCalls, ExactCalls
from ._generated_resources import (
    AsyncCompany,
    AsyncFaction,
    AsyncForum,
    AsyncKey,
    AsyncMarket,
    AsyncProperty,
    AsyncRacing,
    AsyncTorn,
    AsyncUser,
    Company,
    Faction,
    Forum,
    Key,
    Market,
    Property,
    Racing,
    Torn,
    User,
)
from .errors import ValidationError
from .planner import (
    BatchResult,
    SelectionInput,
    TargetValue,
    Version,
    execute_plan,
    execute_plan_async,
    plan_selections,
)
from .runtime import AsyncRuntime, JsonValue, Query, RequestOptions, Runtime


class TornClient(Runtime, ExactCalls):
    """Typed blocking API client. Use as a context manager to close connections."""

    strict: bool = False

    def openapi(self) -> Self:
        """Return the client exposing all generated exact operations."""
        return self

    def exact(
        self, operation: Operation[P, R], params: P | None = None, *,
        query: Query | None = None, options: RequestOptions | None = None,
        strict: bool | None = None,
    ) -> R:
        request = operation_request(operation, params, query)
        response = self.request_response(request, options=options)
        payload = response.json() if request.expect_json else response.text()
        return decode(
            self, operation.response, payload, operation.id, options,
            self.strict if strict is None else strict, raw=response.body, request=request,
        )

    def exact_pages(
        self, operation: Operation[P, R], params: P | None = None, *,
        query: Query | None = None, options: RequestOptions | None = None,
        strict: bool | None = None, backwards: bool = False, max_pages: int | None = None,
    ) -> Iterator[R]:
        if not operation.paginated:
            raise ValidationError(f"{operation.id} has no pagination contract")
        request = operation_request(operation, params, query)
        for response in self.page_responses(
            request, options=options, backwards=backwards, max_pages=max_pages,
        ):
            yield decode(
                self, operation.response, response.json(), operation.id, options,
                self.strict if strict is None else strict, raw=response.body, request=request,
            )

    def exact_items(
        self, operation: Operation[P, R], params: P | None = None, *,
        query: Query | None = None, options: RequestOptions | None = None,
        strict: bool | None = None, backwards: bool = False, max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[object]:
        if limit is not None and limit < 0:
            raise ValidationError("The item limit must be non-negative")
        if limit == 0:
            return
        for page in self.exact_pages(
            operation, params, query=query, options=options, strict=strict,
            backwards=backwards, max_pages=max_pages,
        ):
            for item in items_at(page, operation.item_field):
                yield item
                if limit is not None:
                    limit -= 1
                    if limit == 0:
                        return

    def raw_get(
        self, path: str, query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
    ) -> JsonValue:
        return self.request(raw_request(path, query, version), options=options)

    def raw_typed(
        self, path: str, model: type[R], query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
        strict: bool | None = None,
    ) -> R:
        request = raw_request(path, query, version)
        response = self.request_response(request, options=options)
        return decode(
            self, model, response.json(), path, options,
            self.strict if strict is None else strict, raw=response.body, request=request,
        )

    def raw_pages(
        self, path: str, query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
        backwards: bool = False, max_pages: int | None = None,
    ) -> Iterator[JsonValue]:
        return self.pages(
            raw_request(path, query, version), options=options, backwards=backwards,
            max_pages=max_pages,
        )

    def raw_items(
        self, path: str, query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
        field: str | None = None, backwards: bool = False, max_pages: int | None = None,
        limit: int | None = None,
    ) -> Iterator[JsonValue]:
        return self.items(
            raw_request(path, query, version), options=options, field=field,
            backwards=backwards, max_pages=max_pages, limit=limit,
        )

    def batch(
        self, resource: str, selections: Sequence[SelectionInput], *,
        id: TargetValue | None = None, query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None, version: Version = "auto",
        force_direct: bool = False, options: RequestOptions | None = None, strict: bool | None = None,
        typed: bool = True,
    ) -> BatchResult:
        """Plan and execute selections, preserving each selection's independent outcome."""
        plan = plan_selections(
            resource, selections, id=id, query=query, path_args=path_args,
            version=version, force_direct=force_direct,
        )
        return execute_plan(
            self, plan, options=options, typed=typed,
            strict=self.strict if strict is None else strict,
        )

    def company(self, id: TargetValue | None = None) -> Company:
        return Company(self, id=id)

    def faction(self, id: TargetValue | None = None) -> Faction:
        return Faction(self, id=id)

    def forum(self, id: TargetValue | None = None) -> Forum:
        return Forum(self, id=id)

    def key(self, id: TargetValue | None = None) -> Key:
        return Key(self, id=id)

    def market(self, id: TargetValue | None = None) -> Market:
        return Market(self, id=id)

    def property(self, id: TargetValue | None = None) -> Property:
        return Property(self, id=id)

    def racing(self, id: TargetValue | None = None) -> Racing:
        return Racing(self, id=id)

    def torn(self, id: TargetValue | None = None) -> Torn:
        return Torn(self, id=id)

    def user(self, id: TargetValue | None = None) -> User:
        return User(self, id=id)


class AsyncTornClient(AsyncRuntime, AsyncExactCalls):
    """Typed asyncio API client. Use as an async context manager."""

    strict: bool = False

    def openapi(self) -> Self:
        return self

    async def exact(
        self, operation: Operation[P, R], params: P | None = None, *,
        query: Query | None = None, options: RequestOptions | None = None,
        strict: bool | None = None,
    ) -> R:
        request = operation_request(operation, params, query)
        response = await self.request_response(request, options=options)
        payload = response.json() if request.expect_json else response.text()
        return decode(
            self, operation.response, payload, operation.id, options,
            self.strict if strict is None else strict, raw=response.body, request=request,
        )

    async def exact_pages(
        self, operation: Operation[P, R], params: P | None = None, *,
        query: Query | None = None, options: RequestOptions | None = None,
        strict: bool | None = None, backwards: bool = False, max_pages: int | None = None,
    ) -> AsyncIterator[R]:
        if not operation.paginated:
            raise ValidationError(f"{operation.id} has no pagination contract")
        request = operation_request(operation, params, query)
        async for response in self.page_responses(
            request, options=options, backwards=backwards, max_pages=max_pages,
        ):
            yield decode(
                self, operation.response, response.json(), operation.id, options,
                self.strict if strict is None else strict, raw=response.body, request=request,
            )

    async def exact_items(
        self, operation: Operation[P, R], params: P | None = None, *,
        query: Query | None = None, options: RequestOptions | None = None,
        strict: bool | None = None, backwards: bool = False, max_pages: int | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[object]:
        if limit is not None and limit < 0:
            raise ValidationError("The item limit must be non-negative")
        if limit == 0:
            return
        async for page in self.exact_pages(
            operation, params, query=query, options=options, strict=strict,
            backwards=backwards, max_pages=max_pages,
        ):
            for item in items_at(page, operation.item_field):
                yield item
                if limit is not None:
                    limit -= 1
                    if limit == 0:
                        return

    async def raw_get(
        self, path: str, query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
    ) -> JsonValue:
        return await self.request(raw_request(path, query, version), options=options)

    async def raw_typed(
        self, path: str, model: type[R], query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
        strict: bool | None = None,
    ) -> R:
        request = raw_request(path, query, version)
        response = await self.request_response(request, options=options)
        return decode(
            self, model, response.json(), path, options,
            self.strict if strict is None else strict, raw=response.body, request=request,
        )

    def raw_pages(
        self, path: str, query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
        backwards: bool = False, max_pages: int | None = None,
    ) -> AsyncIterator[JsonValue]:
        return self.pages(
            raw_request(path, query, version), options=options, backwards=backwards,
            max_pages=max_pages,
        )

    def raw_items(
        self, path: str, query: Query | None = None, *,
        version: RawVersion = "v2", options: RequestOptions | None = None,
        field: str | None = None, backwards: bool = False, max_pages: int | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[JsonValue]:
        return self.items(
            raw_request(path, query, version), options=options, field=field,
            backwards=backwards, max_pages=max_pages, limit=limit,
        )

    async def batch(
        self, resource: str, selections: Sequence[SelectionInput], *,
        id: TargetValue | None = None, query: Query | None = None,
        path_args: Mapping[str, TargetValue] | None = None, version: Version = "auto",
        force_direct: bool = False, options: RequestOptions | None = None, strict: bool | None = None,
        typed: bool = True,
    ) -> BatchResult:
        plan = plan_selections(
            resource, selections, id=id, query=query, path_args=path_args,
            version=version, force_direct=force_direct,
        )
        return await execute_plan_async(
            self, plan, options=options, typed=typed,
            strict=self.strict if strict is None else strict,
        )

    def company(self, id: TargetValue | None = None) -> AsyncCompany:
        return AsyncCompany(self, id=id)

    def faction(self, id: TargetValue | None = None) -> AsyncFaction:
        return AsyncFaction(self, id=id)

    def forum(self, id: TargetValue | None = None) -> AsyncForum:
        return AsyncForum(self, id=id)

    def key(self, id: TargetValue | None = None) -> AsyncKey:
        return AsyncKey(self, id=id)

    def market(self, id: TargetValue | None = None) -> AsyncMarket:
        return AsyncMarket(self, id=id)

    def property(self, id: TargetValue | None = None) -> AsyncProperty:
        return AsyncProperty(self, id=id)

    def racing(self, id: TargetValue | None = None) -> AsyncRacing:
        return AsyncRacing(self, id=id)

    def torn(self, id: TargetValue | None = None) -> AsyncTorn:
        return AsyncTorn(self, id=id)

    def user(self, id: TargetValue | None = None) -> AsyncUser:
        return AsyncUser(self, id=id)
