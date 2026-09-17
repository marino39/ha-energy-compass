import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isclose
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..engine.models import InputError
from ..engine.normalize import Interval, aware, finite

_PATH_PART = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


@dataclass(frozen=True)
class EntityBinding:
    entity_id: str
    registry_id: str | None = None
    attribute: str | None = None
    record_value_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize a binding for config-entry storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EntityBinding:
        """Restore a previously selected entity and field."""
        return cls(**value)


@dataclass(frozen=True)
class IntervalBinding:
    entity: EntityBinding
    value_path: str = "value"
    start_path: str | None = None
    end_path: str | None = None
    duration_path: str | None = None
    interval_minutes: float | None = None
    unit: str = "kWh"
    unit_path: str | None = None
    value_kind: str = "energy"
    value_sign: float = 1.0
    source_timezone: str | None = None
    published_path: str | None = None
    max_age_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize observed record paths and interval semantics."""
        return {**asdict(self), "entity": self.entity.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> IntervalBinding:
        """Restore a serialized interval selector mapping."""
        return cls(**{**value, "entity": EntityBinding.from_dict(value["entity"])})


def validate_dependencies(
    bindings: tuple[EntityBinding, ...], own_entity_ids: set[str]
) -> None:
    """Reject direct cycles through this integration's derived entities."""
    for binding in bindings:
        if binding.entity_id in own_entity_ids:
            raise InputError("feedback loop through Energy Compass output")


def resolve_binding(states: Mapping[str, Any], binding: EntityBinding) -> Any:
    """Read a selected state or attribute from a Home Assistant-like snapshot."""
    validate_dependencies((binding,), set())
    state = states.get(binding.entity_id)
    if state is None:
        raise InputError(f"missing entity: {binding.entity_id}")
    if not isinstance(state, Mapping):
        raise InputError("state snapshot must be a mapping")
    if state.get("state") in ("unknown", "unavailable", None):
        raise InputError("source state unavailable")
    if binding.attribute is None:
        return state["state"]
    attributes = state.get("attributes")
    if not isinstance(attributes, Mapping) or binding.attribute not in attributes:
        raise InputError(f"missing attribute: {binding.attribute}")
    return attributes[binding.attribute]


def rebind_entity(
    binding: EntityBinding, registry_entities: Mapping[str, str]
) -> EntityBinding:
    """Track a registry-backed rename without adopting an unrelated entity."""
    if binding.registry_id is None:
        return binding
    entity_id = registry_entities.get(binding.registry_id)
    if entity_id is None:
        raise InputError("missing registered entity; reconfigure source")
    return replace(binding, entity_id=entity_id)


def field(value: Any, path: str) -> Any:
    """Read a selected data path without evaluation or array indexing."""
    if not path or any(not _PATH_PART.fullmatch(part) for part in path.split(".")):
        raise InputError("invalid record field path")
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise InputError(f"missing record field: {path}")
        value = value[part]
    return value


def field_paths(sample: Mapping[str, Any]) -> tuple[str, ...]:
    """Offer selectors only for observed scalar record fields."""
    result = []

    def visit(value: Mapping[str, Any], prefix: str) -> None:
        for key, child in value.items():
            if not isinstance(key, str) or not _PATH_PART.fullmatch(key):
                continue
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, Mapping):
                visit(child, path)
            elif not isinstance(child, (list, tuple, dict)):
                result.append(path)

    visit(sample, "")
    return tuple(result)


def parse_timestamp(value: Any, source_timezone: str | None = None) -> datetime:
    """Resolve an interval timestamp to UTC, rejecting ambiguous local times."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T]24:(00):(00)(.*)", candidate)
        if match:
            candidate = (
                (datetime.fromisoformat(match.group(1)) + timedelta(days=1))
                .date()
                .isoformat()
                + "T00:00:00"
                + match.group(4)
            )
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as err:
            raise InputError("invalid timestamp") from err
    else:
        raise InputError("invalid timestamp")
    if parsed.tzinfo is None:
        if source_timezone is None:
            raise InputError("naive timestamp requires source timezone")
        try:
            zone = ZoneInfo(source_timezone)
        except ZoneInfoNotFoundError as err:
            raise InputError("invalid source timezone") from err
        early = parsed.replace(tzinfo=zone, fold=0)
        late = parsed.replace(tzinfo=zone, fold=1)
        if early.utcoffset() != late.utcoffset():
            raise InputError("ambiguous or nonexistent local timestamp")
        parsed = early
        if parsed.astimezone(UTC).astimezone(zone).replace(
            tzinfo=None
        ) != value_as_naive(value):
            raise InputError("nonexistent local timestamp")
    return aware(parsed, "timestamp").astimezone(UTC)


def value_as_naive(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    candidate = str(value).strip()
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T]24:00:00", candidate)
    if match:
        return datetime.fromisoformat(match.group(1)) + timedelta(days=1)
    return datetime.fromisoformat(candidate)


def _duration(record: Mapping[str, Any], binding: IntervalBinding) -> timedelta:
    if binding.duration_path:
        minutes = finite(field(record, binding.duration_path), "interval duration")
    elif binding.interval_minutes is not None:
        minutes = finite(binding.interval_minutes, "interval duration")
    else:
        raise InputError("interval duration or both endpoints required")
    if minutes <= 0:
        raise InputError("interval duration must be positive")
    return timedelta(minutes=minutes)


def parse_intervals(
    states: Mapping[str, Any], binding: IntervalBinding, now: datetime
) -> tuple[Interval, ...]:
    """Parse record arrays or timestamp-keyed maps into UTC intervals."""
    aware(now, "now")
    state = states.get(binding.entity.entity_id)
    raw = resolve_binding(states, binding.entity)
    if binding.max_age_seconds is not None:
        updated = parse_timestamp(state.get("last_updated"))
        if (now - updated).total_seconds() > finite(
            binding.max_age_seconds, "maximum age"
        ):
            raise InputError("stale source")
    if binding.published_path:
        publication = parse_timestamp(
            field(state, binding.published_path), binding.source_timezone
        )
        if (
            binding.max_age_seconds is not None
            and (now - publication).total_seconds() > binding.max_age_seconds
        ):
            raise InputError("stale publication")
    if isinstance(raw, Mapping):
        records = [
            {"__timestamp__": timestamp, "__value__": value}
            for timestamp, value in raw.items()
        ]
        map_mode = True
    elif isinstance(raw, (list, tuple)):
        records = raw
        map_mode = False
    else:
        raise InputError("interval source must be records or timestamp map")
    result = []
    for record in records:
        if not isinstance(record, Mapping):
            raise InputError("forecast record must be a mapping")
        value = finite(
            record["__value__"] if map_mode else field(record, binding.value_path),
            "interval value",
        )
        direction = finite(binding.value_sign, "interval sign")
        if direction == 0:
            raise InputError("interval sign cannot be zero")
        value *= direction
        start_value = (
            record["__timestamp__"]
            if map_mode
            else (field(record, binding.start_path) if binding.start_path else None)
        )
        end_value = (
            None
            if map_mode
            else (field(record, binding.end_path) if binding.end_path else None)
        )
        if start_value is None and end_value is None:
            raise InputError("interval needs a timestamp")
        start = (
            parse_timestamp(start_value, binding.source_timezone)
            if start_value is not None
            else None
        )
        end = (
            parse_timestamp(end_value, binding.source_timezone)
            if end_value is not None
            else None
        )
        if start is None:
            start = end - _duration(record, binding)
        elif end is None:
            end = start + _duration(record, binding)
        if end <= start:
            raise InputError("interval end must follow start")
        unit = field(record, binding.unit_path) if binding.unit_path else binding.unit
        if binding.value_kind == "power":
            if unit == "W":
                value /= 1000
            elif unit != "kW":
                raise InputError("unsupported power unit")
            value *= (end - start).total_seconds() / 3600
        elif binding.value_kind == "energy":
            if unit == "Wh":
                value /= 1000
            elif unit != "kWh":
                raise InputError("unsupported energy unit")
        elif binding.value_kind == "price":
            if (
                not isinstance(unit, str)
                or unit.split("/", 1)[0] != binding.unit.split("/", 1)[0]
            ):
                raise InputError("price record currency mismatch")
            if unit.endswith("/MWh"):
                value /= 1000
            elif not unit.endswith("/kWh"):
                raise InputError("unsupported price unit")
        else:
            raise InputError("unsupported interval value kind")
        result.append(Interval(start, end, value))
    return tuple(sorted(result, key=lambda row: row.start))


def merge_continuations(
    series: tuple[tuple[Interval, ...], ...],
) -> tuple[Interval, ...]:
    """Deduplicate equal continuation intervals and flag contradictions."""
    merged: dict[tuple[datetime, datetime], Interval] = {}
    for continuation in series:
        for row in continuation:
            key = (row.start, row.end)
            existing = merged.get(key)
            if existing is not None and not isclose(
                existing.value, row.value, rel_tol=1e-9, abs_tol=1e-9
            ):
                raise InputError("conflicting continuation intervals")
            merged[key] = row
    ordered = tuple(sorted(merged.values(), key=lambda row: row.start))
    for previous, current in pairwise(ordered):
        if current.start < previous.end:
            raise InputError("conflicting overlapping intervals")
    return ordered
