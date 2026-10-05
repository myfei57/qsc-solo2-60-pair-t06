"""Pump group status and acknowledged start/stop requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

SPEC_KEY = "clearwell:pump-specs"
STATUS_KEY = "clearwell:pump-status"
COMMAND_KEY = "clearwell:pump-commands"
MAX_PENDING_COMMANDS = 20
DEFAULT_SPECS = [
    {"id": "p1", "min_flow": 10.0, "max_flow": 50.0},
    {"id": "p2", "min_flow": 10.0, "max_flow": 50.0},
]


@dataclass(frozen=True)
class PumpSpec:
    """Physical operating range of one pump."""

    id: str
    min_flow: float
    max_flow: float

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "min_flow": self.min_flow, "max_flow": self.max_flow}


@dataclass(frozen=True)
class PumpTelemetry:
    """The latest independently reported state of one pump."""

    id: str
    running: bool = False
    available: bool = True
    confirmed: bool = True
    flow: float = 0.0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PumpTelemetry":
        pump_id = str(payload.get("id", ""))
        if not pump_id:
            raise ValueError("pump id is required")
        flow = float(payload.get("flow", 0.0))
        if flow < 0:
            raise ValueError("pump flow must be non-negative")
        return cls(
            id=pump_id,
            running=bool(payload.get("running", False)),
            available=bool(payload.get("available", True)),
            confirmed=bool(payload.get("confirmed", True)),
            flow=flow,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "running": self.running,
            "available": self.available,
            "confirmed": self.confirmed,
            "flow": self.flow,
        }


@dataclass(frozen=True)
class PumpStatus:
    """Specification plus the latest telemetry for one pump."""

    spec: PumpSpec
    telemetry: PumpTelemetry

    def as_dict(self) -> dict[str, object]:
        payload = self.spec.as_dict()
        payload.update(self.telemetry.as_dict())
        return payload


@dataclass(frozen=True)
class PumpCommand:
    """A requested pump state change, persisted until telemetry confirms it."""

    request_id: str
    pump_id: str
    action: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "pump_id": self.pump_id,
            "action": self.action,
            "reason": self.reason,
        }


def validate_pump_spec(pump_id: str, min_flow: float, max_flow: float) -> None:
    if not pump_id:
        raise ValueError("pump id is required")
    if min_flow < 0 or max_flow < 0:
        raise ValueError("pump flow limits must be non-negative")
    if max_flow < min_flow:
        raise ValueError("pump maximum flow must not be below minimum flow")


def _load_json(store: Store, key: str, default: object) -> Any:
    raw, present = store.get(key)
    if not present:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


class PumpGroup:
    """Tracks pump capability and enforces command acknowledgement timing."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def specs(self) -> list[PumpSpec]:
        payload = _load_json(self._store, SPEC_KEY, DEFAULT_SPECS)
        if not isinstance(payload, list):
            payload = DEFAULT_SPECS
        specs: list[PumpSpec] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            specs.append(
                PumpSpec(
                    id=str(item.get("id", "")),
                    min_flow=float(item.get("min_flow", 0.0)),
                    max_flow=float(item.get("max_flow", 0.0)),
                )
            )
        return specs

    def replace_specs(self, specs: list[PumpSpec]) -> None:
        if not specs:
            raise ValueError("at least one pump is required")
        ids = [spec.id for spec in specs]
        if len(ids) != len(set(ids)):
            raise ValueError("pump ids must be unique")
        for spec in specs:
            validate_pump_spec(spec.id, spec.min_flow, spec.max_flow)
        self._store.put(SPEC_KEY, json.dumps([spec.as_dict() for spec in specs]))

    def statuses(self) -> list[PumpStatus]:
        specs = self.specs()
        raw = _load_json(self._store, STATUS_KEY, {})
        telemetry_payload = raw.get("pumps", []) if isinstance(raw, dict) else []
        reported = {
            str(item.get("id", "")): item
            for item in telemetry_payload
            if isinstance(item, dict)
        }
        statuses: list[PumpStatus] = []
        for spec in specs:
            item = reported.get(spec.id)
            if item is None:
                telemetry = PumpTelemetry(id=spec.id, confirmed=False)
            else:
                telemetry = PumpTelemetry.from_payload({"id": spec.id, **item})
            statuses.append(PumpStatus(spec=spec, telemetry=telemetry))
        return statuses

    def confirm(self, readings: list[PumpTelemetry]) -> list[PumpStatus]:
        specs = self.specs()
        allowed = {spec.id for spec in specs}
        by_id: dict[str, PumpTelemetry] = {}
        for reading in readings:
            if reading.id not in allowed:
                raise ValueError(f"unknown pump {reading.id}")
            by_id[reading.id] = reading
        statuses = [
            PumpStatus(spec=spec, telemetry=by_id.get(spec.id, PumpTelemetry(id=spec.id, confirmed=False)))
            for spec in specs
        ]
        self._store.put(
            STATUS_KEY,
            json.dumps({"pumps": [status.telemetry.as_dict() for status in statuses]}),
        )
        self._reconcile_commands(statuses)
        return statuses

    def confirmed(self) -> bool:
        return all(status.telemetry.confirmed for status in self.statuses())

    def current_range(self) -> tuple[float, float]:
        minimum = 0.0
        maximum = 0.0
        for status in self.statuses():
            telemetry = status.telemetry
            if telemetry.confirmed and telemetry.available and telemetry.running:
                minimum += status.spec.min_flow
                maximum += max(status.spec.max_flow, status.spec.min_flow)
        return minimum, maximum

    def total_capacity(self) -> float:
        return sum(status.spec.max_flow for status in self.statuses())

    def pending_commands(self) -> list[PumpCommand]:
        commands: list[PumpCommand] = []
        for item in load_commands(self._store, COMMAND_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            commands.append(
                PumpCommand(
                    request_id=str(payload.get("request_id", "")),
                    pump_id=str(payload.get("pump_id", "")),
                    action=str(payload.get("action", "")),
                    reason=str(payload.get("reason", "")),
                )
            )
        return commands

    def command(self, request_id: str, pump_id: str, action: str, reason: str) -> PumpCommand:
        if action not in {"start", "stop"}:
            raise ValueError("pump action must be start or stop")
        if pump_id not in {spec.id for spec in self.specs()}:
            raise ValueError(f"unknown pump {pump_id}")
        for existing in self.pending_commands():
            if existing.request_id == request_id:
                return existing
            if existing.pump_id == pump_id and existing.action == action:
                return existing
        command = PumpCommand(request_id=request_id, pump_id=pump_id, action=action, reason=reason)
        pending = self.pending_commands()
        pending.append(command)
        self._store.put(
            COMMAND_KEY, json.dumps([item.as_dict() for item in pending[-MAX_PENDING_COMMANDS:]])
        )
        return command

    def request_for_outlet(self, target_flow: float, request_id: str, reason: str) -> PumpCommand | None:
        """Request one pump change when the acknowledged group cannot hold flow."""

        if not self.confirmed():
            return None
        minimum, maximum = self.current_range()
        if minimum <= target_flow <= maximum:
            return None
        pending = {(command.pump_id, command.action) for command in self.pending_commands()}
        if target_flow > maximum:
            for status in self.statuses():
                telemetry = status.telemetry
                if telemetry.confirmed and telemetry.available and not telemetry.running:
                    if (status.spec.id, "start") not in pending:
                        return self.command(request_id, status.spec.id, "start", reason)
        else:
            candidates = [
                status
                for status in self.statuses()
                if status.telemetry.confirmed
                and status.telemetry.available
                and status.telemetry.running
                and (status.spec.id, "stop") not in pending
            ]
            candidates.sort(key=lambda status: (status.spec.max_flow, status.spec.id), reverse=True)
            if candidates:
                return self.command(request_id, candidates[0].spec.id, "stop", reason)
        return None

    def state(self) -> dict[str, object]:
        minimum, maximum = self.current_range()
        return {
            "confirmed": self.confirmed(),
            "min_flow": minimum,
            "max_flow": maximum,
            "total_capacity": self.total_capacity(),
            "pumps": [status.as_dict() for status in self.statuses()],
            "pending_commands": [command.as_dict() for command in self.pending_commands()],
        }

    def describe(self) -> str:
        minimum, maximum = self.current_range()
        return (
            f"pumps={len(self.statuses())} confirmed={self.confirmed()} "
            f"range={minimum:.4f}..{maximum:.4f} pending={len(self.pending_commands())}"
        )

    def _reconcile_commands(self, statuses: list[PumpStatus]) -> None:
        state = {status.spec.id: status.telemetry for status in statuses}
        remaining: list[PumpCommand] = []
        for command in self.pending_commands():
            telemetry = state.get(command.pump_id)
            if telemetry is None:
                remaining.append(command)
                continue
            achieved = command.action == "start" and telemetry.running
            achieved |= command.action == "stop" and not telemetry.running
            if not achieved:
                remaining.append(command)
        self._store.put(
            COMMAND_KEY, json.dumps([command.as_dict() for command in remaining])
        )
