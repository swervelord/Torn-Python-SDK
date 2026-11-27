"""Shared key, route, and quota state for synchronous and asynchronous clients."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock

from ._runtime_types import Key, KeyState, KeyStatus, RuntimeConfig, TimeSource
from .errors import AccessError, AccessLevel, ApiError, NoUsableKeysError, ValidationError

_ACCESS_RANK = {"public": 1, "minimal": 2, "limited": 3, "full": 4}


def key_id(key: str) -> str:
    return sha256(key.encode("ascii")).hexdigest()


class Window:
    def __init__(self, limit: int, length: float) -> None:
        self.limit = limit
        self.length = length
        self.sends: deque[float] = deque()
        self.blocked_until = 0.0
        self.last_error: ApiError | None = None

    def remaining(self, now: float) -> int:
        while self.sends and self.sends[0] + self.length <= now:
            self.sends.popleft()
        return 0 if self.blocked_until > now else max(0, self.limit - len(self.sends))

    def ready_at(self, now: float) -> float:
        self.remaining(now)
        capacity_time = (
            self.sends[len(self.sends) - self.limit] + self.length
            if len(self.sends) >= self.limit
            else now
        )
        return max(capacity_time, self.blocked_until)

    def block(self, now: float) -> None:
        self.remaining(now)
        earliest = self.sends[0] + self.length if self.sends else now + self.length
        self.blocked_until = max(self.blocked_until, earliest)


@dataclass(eq=False)
class Slot:
    id: str
    window: Window
    access: AccessLevel | None = None
    owner: str | int | None = None
    disabled_until: float = 0.0
    last_error: ApiError | None = None


@dataclass(frozen=True)
class Reservation:
    index: int
    route: str
    slot: Slot = field(repr=False)


class Pool:
    def __init__(
        self,
        keys: Iterable[Key],
        route_limits: dict[str, int],
        config: RuntimeConfig,
        clock: TimeSource,
        *,
        check_access: bool,
        detect_key_owners: bool,
    ) -> None:
        self.clock = clock
        self.config = config
        self.check_access = check_access
        self.detect_key_owners = detect_key_owners
        self.lock = RLock()
        self.cursor = 0
        self.keys: list[Key] = []
        self.slots: list[Slot] = []
        self.by_key: dict[str, Slot] = {}
        owners: dict[str, Window] = {}
        for key in keys:
            identifier = key_id(key._reveal())
            if identifier in self.by_key:
                continue
            owner = str(key.owner) if key.owner is not None else None
            window = self.new_key_window()
            if owner is not None:
                window = owners.setdefault(owner, window)
            slot = Slot(identifier, window, key.access, key.owner)
            self.keys.append(key)
            self.slots.append(slot)
            self.by_key[identifier] = slot
        self.route_windows = {
            name: Window(limit, config.rate_window) for name, limit in route_limits.items()
        }
        self.last_error: ApiError | None = None

    def new_key_window(self) -> Window:
        return Window(self.config.key_per_minute, self.config.rate_window)

    def explicit_slot(self, key: Key) -> Slot:
        identifier = key_id(key._reveal())
        with self.lock:
            slot = self.by_key.get(identifier)
            if slot is None:
                slot = Slot(identifier, self.new_key_window())
                self.by_key[identifier] = slot
            return slot

    def _key_candidates(
        self,
        explicit: Slot | None,
        preferred: Slot | None,
        skipped: set[str],
        operation: str,
        access: AccessLevel | None,
        last: ApiError | None,
    ) -> list[tuple[int, Slot]]:
        if explicit is not None:
            candidates = [(-1, explicit)]
        elif preferred is not None:
            candidates = [(self.slots.index(preferred), preferred)]
        else:
            candidates = list(enumerate(self.slots))
            candidates = candidates[self.cursor :] + candidates[: self.cursor]
        wall_now = self.clock.time()
        candidates = [
            (index, slot)
            for index, slot in candidates
            if slot.id not in skipped and slot.disabled_until <= wall_now
        ]
        if not candidates:
            raise NoUsableKeysError(last or self.last_error)
        if self.check_access and access is not None:
            allowed = [
                (index, slot)
                for index, slot in candidates
                if _ACCESS_RANK.get(slot.access or "", math.inf) >= _ACCESS_RANK.get(access, 0)
            ]
            if not allowed:
                raise AccessError(
                    operation,
                    access,
                    tuple(slot.access for _, slot in candidates if slot.access is not None),
                )
            candidates = allowed
        return candidates

    def peek_key(
        self,
        explicit: Slot | None,
        preferred: Slot | None,
        skipped: set[str],
        operation: str,
        access: AccessLevel | None,
        last: ApiError | None,
    ) -> tuple[int, Slot]:
        with self.lock:
            choices = self._key_candidates(explicit, preferred, skipped, operation, access, last)
            now = self.clock.monotonic()
            return min(choices, key=lambda pair: pair[1].window.ready_at(now))

    def reserve(
        self,
        *,
        explicit: Slot | None,
        preferred: Slot | None,
        skipped: set[str],
        pinned_route: str | None,
        skipped_routes: set[str],
        operation: str,
        access: AccessLevel | None,
        last: ApiError | None,
    ) -> Reservation | float:
        with self.lock:
            choices = self._key_candidates(explicit, preferred, skipped, operation, access, last)
            now = self.clock.monotonic()
            index, slot = min(choices, key=lambda pair: pair[1].window.ready_at(now))
            routes = (pinned_route,) if pinned_route is not None else tuple(self.route_windows)
            available_routes = [
                name
                for name in routes
                if name not in skipped_routes and self.route_windows[name].blocked_until <= now
            ]
            if not available_routes:
                if last is not None and last.code.action == "rotate_route":
                    raise last
                for name in routes:
                    if (error := self.route_windows[name].last_error) is not None:
                        raise error
                raise ValidationError("No usable Torn egress routes")
            route = min(available_routes, key=lambda name: self.route_windows[name].ready_at(now))
            window = self.route_windows[route]
            wait = max(slot.window.ready_at(now), window.ready_at(now)) - now
            if wait > 0:
                return float(wait)
            slot.window.sends.append(now)
            window.sends.append(now)
            if index >= 0:
                self.cursor = (index + 1) % len(self.slots)
            return Reservation(index, route, slot)

    def reject(self, reservation: Reservation, error: ApiError) -> None:
        with self.lock:
            action = error.code.action
            slot = reservation.slot
            if action == "rotate_key":
                self.last_error = error
                slot.last_error = error
                if error.code.disable_key == "client_lifetime":
                    slot.disabled_until = math.inf
                elif error.code.disable_key == "until_utc_midnight":
                    slot.disabled_until = (math.floor(self.clock.time() / 86400) + 1) * 86400
            elif action == "wait_rate_window":
                slot.window.block(self.clock.monotonic())
            elif action == "rotate_route":
                now = self.clock.monotonic()
                window = self.route_windows[reservation.route]
                window.blocked_until = now + self.config.rate_window
                window.last_error = error

    def states(self) -> tuple[KeyState, ...]:
        with self.lock:
            now = self.clock.monotonic()
            wall_now = self.clock.time()
            entries: list[KeyState] = []
            for index, (key, slot) in enumerate(zip(self.keys, self.slots)):
                state: KeyStatus = "healthy"
                until: float | None = None
                reason: str | None = None
                remaining = slot.window.remaining(now)
                if slot.disabled_until > wall_now:
                    state = "disabled"
                    until = slot.disabled_until if math.isfinite(slot.disabled_until) else None
                    reason = slot.last_error.code.label if slot.last_error is not None else None
                    remaining = 0
                elif remaining == 0:
                    state = "rate_limited"
                    until = wall_now + slot.window.ready_at(now) - now
                entries.append(
                    KeyState(
                        index, key.hint, state, remaining, slot.access, slot.owner, until, reason
                    )
                )
            return tuple(entries)

    def preload(self, key: str, sends: Iterable[float]) -> None:
        with self.lock:
            slot = self.by_key.get(key_id(key))
            if slot is None:
                raise ValidationError("Cannot preload an unconfigured Torn API key")
            slot.window.sends = deque(sorted((*slot.window.sends, *sends)))

    def update_key(self, key: Key, owner: int | None, access: AccessLevel | None) -> None:
        with self.lock:
            slot = self.by_key[key_id(key._reveal())]
            if access is not None:
                slot.access = access
            if owner is None:
                return
            slot.owner = owner
            if not self.detect_key_owners:
                return
            for peer in self.slots:
                old_window = peer.window
                if str(peer.owner) != str(owner) or old_window is slot.window:
                    continue
                slot.window.sends = deque(sorted((*slot.window.sends, *old_window.sends)))
                slot.window.blocked_until = max(slot.window.blocked_until, old_window.blocked_until)
                for target in self.slots:
                    if target.window is old_window:
                        target.window = slot.window
