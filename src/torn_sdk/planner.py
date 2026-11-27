"""Plan Torn selections and retain each selection's outcome independently."""

from __future__ import annotations

import re
from collections.abc import Callable, Generator, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Generic, Literal, TypeVar, cast, get_args
from urllib.parse import quote

from pydantic import AliasChoices, BaseModel

from ._contracts import Selection
from ._decode import decode_response
from ._routing import FORCE_DIRECT, ID_DOMAINS, V1_CATALOG, V2_ENUM_LEGACY
from .errors import AccessLevel, ApiError, HttpError, TornError, TornErrorCode, ValidationError
from .operations import OPERATIONS
from .runtime import (
    AsyncRuntime,
    ExecutionContext,
    JsonValue,
    Key,
    Query,
    QueryValue,
    Request,
    RequestOptions,
    RequestTrace,
    Response,
    Runtime,
)
from .selections import SELECTIONS

T = TypeVar("T")
SelectionInput = str | Selection[object]
TargetValue = str | int | Iterable[str | int]
Version = Literal["auto", "v1", "v2", "legacy"]
Media = Literal["json", "csv"]
_RESOURCE_NAMES = frozenset(resource for resource, _ in SELECTIONS)
_ACCESS_ORDER = {value: rank for rank, value in enumerate(("public", "minimal", "limited", "full"))}
_NEUTRAL = frozenset(("lookup", "timestamp"))
_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True)
class PlannedCall:
    """One logical HTTP call, with a relative path and API version."""

    resource: str
    operation: str
    path: str
    selections: tuple[str, ...]
    query: Query = field(default_factory=dict, repr=False)
    version: Literal["v1", "v2"] = "v2"
    media: Media = "json"
    access: AccessLevel | None = None
    cache_ttl: float | None = None
    split: tuple[PlannedCall, ...] = field(default=(), repr=False)
    alternate: tuple[PlannedCall, ...] = field(default=(), repr=False)

    @property
    def url_path(self) -> str:
        return f"/v2{self.path}" if self.version == "v2" else self.path


@dataclass(frozen=True)
class RequestPlan:
    resource: str
    calls: tuple[PlannedCall, ...]
    _query_key: Key | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SelectionResult(Generic[T]):
    """An independently decoded contribution, or its error."""

    value: T | None = field(default=None, repr=False)
    raw: JsonValue | None = field(default=None, repr=False)
    error: TornError | None = None


@dataclass(frozen=True)
class ExecutionReport:
    calls: tuple[ExecutedCall, ...]
    traces: tuple[RequestTrace, ...]


@dataclass(frozen=True)
class ExecutedCall:
    """The credential-free description of an attempted request."""

    operation: str
    path: str
    selections: tuple[str, ...]
    version: Literal["v1", "v2"]
    media: Media


@dataclass(frozen=True)
class BatchResult:
    """Results from a raw or typed selection batch, including partial successes."""

    resource: str
    data: dict[str, JsonValue] = field(repr=False)
    results: Mapping[str, SelectionResult[object]] = field(repr=False)
    report: ExecutionReport
    typed: bool = True

    @property
    def errors(self) -> dict[str, TornError]:
        return {
            name: result.error for name, result in self.results.items() if result.error is not None
        }

    def get(self, selection: Selection[T]) -> T:
        """Return the concrete response type associated with a selected token."""
        if selection.resource != self.resource:
            raise KeyError(selection.name)
        result = self.results[selection.name]
        if result.error is not None:
            raise result.error
        if not self.typed:
            raise ValidationError("a raw batch has no typed selection values")
        return cast("T", result.value)

    def raise_for_errors(self) -> BatchResult:
        """Raise the first selection error, if any."""
        if error := next(iter(self.errors.values()), None):
            raise error
        return self

    def decode(self, response: type[T], *, strict: bool = False) -> T:
        """Decode the merged raw response into an application-supplied type."""
        return decode_response(response, self.data, operation=self.resource, strict=strict)


def _normalize(name: str) -> str:
    return name.strip().lower()


def _target(value: TargetValue) -> str:
    if isinstance(value, (str, int)):
        text = str(value)
    else:
        text = ",".join(str(item) for item in value)
    if not text:
        raise ValidationError("selection target cannot be empty")
    return text


def _get_selection(resource: str, name: str) -> Selection[object] | None:
    return SELECTIONS.get((resource, name))


def _target_domain(
    resource: str, name: str, selection: Selection[object]
) -> tuple[str, str, bool, bool]:
    if name in _NEUTRAL:
        return "neutral", "id", False, False
    if selection.legacy or ((resource, name) in V2_ENUM_LEGACY and not selection.endpoints):
        return "id", "id", False, False
    required_sets = [tuple(_PATH_PARAMETER.findall(endpoint.path)) for endpoint in selection.endpoints]
    significant = next((args[0] for args in required_sets if args and args[0] != "id"), None)
    parameter = significant or ("id" if any(required_sets) else "self")
    domain, parameter, in_query = ID_DOMAINS.get((resource, name), (parameter, parameter, False))
    return domain, parameter, in_query, bool(required_sets and all(required_sets) and not in_query)


def _metadata(resource: str, names: tuple[str, ...]) -> tuple[AccessLevel | None, float | None]:
    access_levels: list[AccessLevel] = []
    ttl_seconds: list[float] = []
    cacheable = True
    for name in names:
        selection = _get_selection(resource, name)
        if selection is None:
            cacheable = False
            continue
        if selection.access in _ACCESS_ORDER:
            access_levels.append(selection.access)
        if not selection.endpoints:
            cacheable = False
        for endpoint in selection.endpoints:
            operation = OPERATIONS.get(endpoint.id)
            if operation is None or operation.cache_ttl is None:
                cacheable = False
            else:
                ttl_seconds.append(operation.cache_ttl)
    return (
        max(access_levels, key=_ACCESS_ORDER.__getitem__, default=None),
        min(ttl_seconds) if ttl_seconds and cacheable else None,
    )


def _generic_operation(resource: str) -> str:
    return f"get{resource.capitalize()}Generic"


def _legacy_call(
    resource: str,
    names: tuple[str, ...],
    target: str | None,
    query: tuple[tuple[str, QueryValue], ...],
    *,
    known_only: bool,
) -> PlannedCall:
    if known_only:
        for name in names:
            if (resource, name) not in V1_CATALOG:
                raise ValidationError(f"{resource}.{name} is absent from the v1 selection catalog")
    access, ttl = _metadata(resource, names)
    return PlannedCall(
        resource,
        "v1Selections",
        f"/{resource}/{quote(target or '', safe=',')}",
        names,
        (*query, ("selections", ",".join(names))),
        version="v1",
        access=access,
        cache_ttl=ttl,
    )


def _generic_call(
    resource: str,
    names: tuple[str, ...],
    target: str | None,
    query: tuple[tuple[str, QueryValue], ...],
) -> PlannedCall:
    access, ttl = _metadata(resource, names)
    params = list(query)
    if target is not None:
        params.append(("id", target))
    params.append(("selections", ",".join(names)))
    return PlannedCall(
        resource,
        _generic_operation(resource),
        f"/{resource}",
        names,
        tuple(params),
        access=access,
        cache_ttl=ttl,
    )


def _direct_call(
    resource: str,
    selection: Selection[object],
    target: str | None,
    parameter: str,
    query_target: bool,
    query: tuple[tuple[str, QueryValue], ...],
) -> PlannedCall:
    if query_target and target is not None:
        if any(key == parameter for key, _ in query):
            raise ValidationError(f"specify {parameter!r} only once")
        query = (*query, (parameter, target))
        target = None
    for endpoint in selection.endpoints:
        path_parameters = _PATH_PARAMETER.findall(endpoint.path)
        expected = [parameter] if target is not None else []
        if path_parameters != expected:
            continue
        path = endpoint.path if target is None else endpoint.path.replace(
            "{" + parameter + "}", quote(target, safe=",")
        )
        operation = OPERATIONS[endpoint.id]
        return PlannedCall(
            resource,
            endpoint.id,
            path,
            (selection.name,),
            query,
            media="csv" if selection.csv else "json",
            access=operation.access,
            cache_ttl=operation.cache_ttl,
        )
    raise ValidationError(f"{resource}.{selection.name} has no exact endpoint for this target")


def _can_use_generic(selection: Selection[object], query: Mapping[str, QueryValue]) -> bool:
    key = (selection.resource, selection.name)
    compatible = selection.generic and not selection.csv and key not in FORCE_DIRECT
    generic = OPERATIONS[_generic_operation(selection.resource)]
    generic_names = {item.name for item in generic.parameters}
    # Only validate a query parameter required by every exact route. Variants can
    # have different optional inputs, and unknown future filters stay on the wire.
    if not selection.endpoints:
        return compatible
    required = [
        {p.name: p for p in OPERATIONS[endpoint.id].parameters if p.required and p.location == "query"}
        for endpoint in selection.endpoints
    ]
    shared = set.intersection(*(set(route) for route in required))
    for name in sorted(shared):
        label = f"{selection.resource}.{selection.name}"
        if name not in query or query[name] is None:
            raise ValidationError(f"{label} requires query parameter {name!r}")
        choices = required[0][name].choices
        if choices and query[name] not in choices:
            raise ValidationError(f"{label} received an invalid value for {name!r}")
        if name not in generic_names:
            compatible = False
    return compatible


def plan_selections(
    resource: str,
    selections: Iterable[SelectionInput],
    *,
    id: TargetValue | None = None,
    query: Query | None = None,
    path_args: Mapping[str, TargetValue] | None = None,
    version: Version = "auto",
    force_direct: bool = False,
) -> RequestPlan:
    """Group compatible v2 selections and route dedicated, CSV and v1 calls."""
    resource = _normalize(resource)
    if resource not in _RESOURCE_NAMES:
        raise ValidationError(f"unknown Torn resource {resource!r}")
    names: dict[str, None] = {}
    for token in selections:
        if isinstance(token, Selection):
            if token.resource != resource:
                raise ValidationError("all batch selections must belong to the same resource")
            names[token.name] = None
        else:
            name = _normalize(token)
            if not name or "," in name:
                raise ValidationError("each selection must be a single, nonempty name")
            names[name] = None
    if not names:
        raise ValidationError("at least one Torn selection is required")
    if version not in ("auto", "v1", "v2", "legacy"):
        raise ValidationError("unsupported Torn selection routing version")

    entries = tuple(query.items() if isinstance(query, Mapping) else query or ())
    if any(name == "selections" for name, _ in entries):
        raise ValidationError("specify selections using the selections argument")
    ids = [value for name, value in entries if name == "id" and value is not None]
    key_arg = next((value for name, value in entries if name.casefold() == "key" and value is not None), None)
    if key_arg is not None and not isinstance(key_arg, str):
        raise ValidationError("a legacy query key must be a string")
    query_key = Key(key_arg) if key_arg is not None else None
    params = tuple((name, value) for name, value in entries if name.casefold() != "key" and name != "id")
    named_params = dict(params)
    targets = dict(path_args or {})
    if ids:
        if id is not None or len(ids) > 1:
            raise ValidationError("specify id only once")
        if isinstance(ids[0], (float, bool)):
            raise ValidationError("id must be a string, integer or list of IDs")
        id = cast("TargetValue", ids[0])
    if id is not None:
        if "id" in targets:
            raise ValidationError("specify id only once")
        targets["id"] = id
    if len(targets) > 1:
        raise ValidationError("a selection batch can use only one target ID domain")
    target_name, target_value = next(iter(targets.items()), ("id", None))
    target = _target(target_value) if target_value is not None else None

    def legacy_call(tokens: tuple[str, ...], *, known_only: bool) -> PlannedCall:
        if target is not None and target_name != "id":
            raise ValidationError("v1 selections require a resource ID")
        return _legacy_call(resource, tokens, target, params, known_only=known_only)

    def fallback(call: PlannedCall) -> PlannedCall:
        if version == "v2":
            return call
        if call.version == "v1":
            try:
                rerouted = plan_selections(
                    resource, call.selections, query=params, version="v2",
                    path_args={target_name: target} if target is not None else None,
                    force_direct=force_direct,
                )
            except ValidationError:
                return call
            return replace(call, alternate=rerouted.calls)
        if target is not None and target_name != "id":
            return call
        other = legacy_call(call.selections, known_only=False)
        return replace(call, alternate=(split(other, allow_alternate=False),))

    def split(call: PlannedCall, *, allow_alternate: bool = True) -> PlannedCall:
        if len(call.selections) < 2:
            return call
        children: list[PlannedCall] = []
        for token in call.selections:
            one = (token,)
            if call.version == "v1":
                child = legacy_call(one, known_only=False)
            else:
                child = _generic_call(resource, one, _target_for_generic(one), params)
            children.append(fallback(child) if allow_alternate else child)
        return replace(call, split=tuple(children))

    def _target_for_generic(tokens: tuple[str, ...]) -> str | None:
        return target if any(name not in _NEUTRAL for name in tokens) else None

    if version == "v1":
        return RequestPlan(resource, (split(fallback(legacy_call(tuple(names), known_only=True))),), query_key)

    generic_names: list[str] = []
    legacy_names: list[str] = []
    standalone: list[PlannedCall] = []
    direct: list[PlannedCall] = []
    domain = None
    for name in names:
        key = (resource, name)
        selection = _get_selection(resource, name)
        if selection is None or selection.legacy:
            recognized_v1 = key in V1_CATALOG
            server_fallback = key in V2_ENUM_LEGACY
            if version != "v2" and recognized_v1 and (not server_fallback or version == "legacy"):
                legacy_names.append(name)
                continue
            if version == "legacy":
                raise ValidationError(f"unknown selection {resource}.{name}")
            generic_names.append(name)
            continue
        if version == "legacy" and key in V2_ENUM_LEGACY and key in V1_CATALOG:
            legacy_names.append(name)
            continue
        scope, parameter, query_target, required = _target_domain(resource, name, selection)
        if required and target is None:
            raise ValidationError(f"{resource}.{name} requires target {parameter!r}")
        if target is not None and scope != "neutral":
            # A server-side v1 fallback can resolve a target where v2 has only
            # a self-scoped exact endpoint, e.g. another user's bazaar.
            if scope == "self" and key in V2_ENUM_LEGACY:
                scope, parameter = "id", "id"
            if scope == "self" or target_name not in ("id", parameter, scope):
                raise ValidationError(f"{resource}.{name} does not accept this target")
            if domain is not None and scope != domain:
                raise ValidationError("batch selections require incompatible target ID domains")
            domain = scope
        if _can_use_generic(selection, named_params) and not force_direct:
            if selection.standalone:
                one = (name,)
                standalone.append(fallback(_generic_call(resource, one, _target_for_generic(one), params)))
            else:
                generic_names.append(name)
        else:
            exact_target = None if scope == "neutral" else target
            call = _direct_call(resource, selection, exact_target, parameter, query_target, params)
            direct.append(fallback(call))

    calls = []
    if generic_names:
        tokens = tuple(generic_names)
        calls.append(split(fallback(_generic_call(resource, tokens, _target_for_generic(tokens), params))))
    calls.extend(standalone)
    calls.extend(direct)
    if legacy_names:
        calls.append(split(fallback(legacy_call(tuple(legacy_names), known_only=True))))
    return RequestPlan(resource, tuple(calls), query_key)


def _merge(target: dict[str, JsonValue], source: Mapping[str, JsonValue]) -> None:
    for key, value in source.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value)
        else:
            target[key] = deepcopy(value)


def _response_type(call: PlannedCall, name: str) -> object:
    selection = _get_selection(call.resource, name)
    if selection is None:
        return None
    if call.version == "v1":
        return selection.legacy_response or selection.response
    if call.operation not in (_generic_operation(call.resource), "v1Selections"):
        return OPERATIONS[call.operation].response
    return selection.response


@lru_cache(maxsize=None)
def _model_roots(response: object) -> frozenset[str]:
    if isinstance(response, type) and issubclass(response, BaseModel):
        aliases = set()
        for name, field in response.model_fields.items():
            aliases.add(field.alias or name)
            if isinstance(field.validation_alias, AliasChoices):
                aliases.update(alias for alias in field.validation_alias.choices if isinstance(alias, str))
        return frozenset(aliases)
    roots: set[str] = set()
    for option in get_args(response):
        roots.update(_model_roots(option))
    return frozenset(roots)


def _finish_call(
    call: PlannedCall,
    response: Response,
    result: dict[str, SelectionResult[object]],
    merged: dict[str, JsonValue],
    *,
    typed: bool,
    strict: bool,
    redact: Callable[[str], str],
) -> None:
    try:
        payload = response.text() if call.media == "csv" else response.json()
    except TornError as error:
        result.update((name, SelectionResult(error=error)) for name in call.selections)
        return
    if isinstance(payload, dict):
        _merge(merged, payload)
    else:
        merged.update((name, payload) for name in call.selections)
    models = {name: _response_type(call, name) for name in call.selections} if typed else {}
    claimed = {name: _model_roots(model) for name, model in models.items()}
    roots = set().union(*claimed.values())
    for name in call.selections:
        model = models.get(name)
        projected = payload
        if len(call.selections) > 1 and isinstance(payload, dict):
            own_roots = claimed.get(name, frozenset()) or frozenset({name, "_metadata"})
            other_roots = roots | set(call.selections)
            projected = {key: value for key, value in payload.items() if key in own_roots or key not in other_roots}
        if model is None:
            result[name] = SelectionResult(projected, projected)
            continue
        try:
            value = decode_response(
                cast("type[object]", model), projected, operation=f"{call.resource}.{name}",
                strict=strict, raw=response.body, redact=redact,
            )
        except TornError as error:
            result[name] = SelectionResult(raw=projected, error=error)
        else:
            result[name] = SelectionResult(value, projected)


@dataclass(frozen=True)
class _Send:
    call: PlannedCall
    context: ExecutionContext
    request: Request


def _plan_steps(
    plan: RequestPlan,
    context: ExecutionContext,
    results: dict[str, SelectionResult[object]],
    merged: dict[str, JsonValue],
    executed: list[ExecutedCall],
    *,
    typed: bool,
    strict: bool,
    redact: Callable[[str, Request], str],
) -> Generator[_Send, Response | TornError, None]:
    def execute(call: PlannedCall, send_context: ExecutionContext) -> Generator[_Send, Response | TornError, None]:
        query = call.query
        if plan._query_key is not None:
            entries = tuple(query.items() if isinstance(query, Mapping) else query)
            query = (*entries, ("key", plan._query_key._reveal()))
        request = Request(
            call.url_path, query, call.operation, call.access, call.cache_ttl,
            expect_json=call.media == "json",
        )
        executed.append(ExecutedCall(
            redact(call.operation, request), redact(call.path, request),
            tuple(redact(name, request) for name in call.selections), call.version, call.media,
        ))
        reply: Response | TornError = yield _Send(call, send_context, request)
        if isinstance(reply, TornError):
            code = reply.code if isinstance(reply, ApiError) else None
            migrate = (
                code == TornErrorCode.ONLY_AVAILABLE_IN_API_V1 and call.version == "v2"
                or code == TornErrorCode.ONLY_AVAILABLE_IN_API_V2 and call.version == "v1"
            )
            if migrate and call.alternate:
                pinned = send_context.pin_key()
                for alternate in call.alternate:
                    yield from execute(alternate, pinned)
            elif call.split and (
                code == TornErrorCode.WRONG_FIELDS
                or isinstance(reply, HttpError) and reply.status in (400, 404, 501)
            ):
                pinned = send_context.pin_key()
                for child in call.split:
                    yield from execute(child, pinned)
            else:
                results.update((name, SelectionResult(error=reply)) for name in call.selections)
            return
        _finish_call(call, reply, results, merged, typed=typed, strict=strict,
                     redact=lambda text: redact(text, request))

    for call in plan.calls:
        yield from execute(call, context)


def execute_plan(
    runtime: Runtime,
    plan: RequestPlan,
    *,
    options: RequestOptions | None = None,
    typed: bool = False,
    strict: bool = False,
) -> BatchResult:
    """Execute a selection plan synchronously and retain all outcomes."""
    context = runtime.new_context()
    results: dict[str, SelectionResult[object]] = {}
    merged: dict[str, JsonValue] = {}
    executed: list[ExecutedCall] = []
    steps = _plan_steps(plan, context, results, merged, executed, typed=typed, strict=strict,
                        redact=lambda text, request: runtime.redact(text, options, request))
    try:
        step = next(steps)
        while True:
            reply: Response | TornError
            try:
                reply = runtime.request_response(step.request, options=options, context=step.context)
            except TornError as error:
                reply = error
            step = steps.send(reply)
    except StopIteration:
        return BatchResult(
            plan.resource, merged, results, ExecutionReport(tuple(executed), context.traces), typed,
        )


async def execute_plan_async(
    runtime: AsyncRuntime,
    plan: RequestPlan,
    *,
    options: RequestOptions | None = None,
    typed: bool = False,
    strict: bool = False,
) -> BatchResult:
    """Execute a selection plan asynchronously and retain all outcomes."""
    context = runtime.new_context()
    results: dict[str, SelectionResult[object]] = {}
    merged: dict[str, JsonValue] = {}
    executed: list[ExecutedCall] = []
    steps = _plan_steps(plan, context, results, merged, executed, typed=typed, strict=strict,
                        redact=lambda text, request: runtime.redact(text, options, request))
    try:
        step = next(steps)
        while True:
            reply: Response | TornError
            try:
                reply = await runtime.request_response(step.request, options=options, context=step.context)
            except TornError as error:
                reply = error
            step = steps.send(reply)
    except StopIteration:
        return BatchResult(
            plan.resource, merged, results, ExecutionReport(tuple(executed), context.traces), typed,
        )


def batch(
    runtime: Runtime,
    first: SelectionInput,
    *tokens: SelectionInput,
    id: TargetValue | None = None,
    query: Query | None = None,
    path_args: Mapping[str, TargetValue] | None = None,
    version: Version = "auto",
    force_direct: bool = False,
    options: RequestOptions | None = None,
    strict: bool = False,
) -> BatchResult:
    """Fetch typed selections together. The first token establishes the resource."""
    if not isinstance(first, Selection):
        raise ValidationError("pass a typed selection as the first batch argument")
    plan = plan_selections(first.resource, (first, *tokens), id=id, query=query, path_args=path_args,
                           version=version, force_direct=force_direct)
    return execute_plan(runtime, plan, options=options, strict=strict, typed=True)


async def batch_async(
    runtime: AsyncRuntime,
    first: SelectionInput,
    *tokens: SelectionInput,
    id: TargetValue | None = None,
    query: Query | None = None,
    path_args: Mapping[str, TargetValue] | None = None,
    version: Version = "auto",
    force_direct: bool = False,
    options: RequestOptions | None = None,
    strict: bool = False,
) -> BatchResult:
    """Fetch typed selections together asynchronously."""
    if not isinstance(first, Selection):
        raise ValidationError("pass a typed selection as the first batch argument")
    plan = plan_selections(first.resource, (first, *tokens), id=id, query=query, path_args=path_args,
                           version=version, force_direct=force_direct)
    return await execute_plan_async(runtime, plan, options=options, strict=strict, typed=True)
