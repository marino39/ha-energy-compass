from dataclasses import dataclass
from datetime import datetime

from ..engine.models import InputError
from ..engine.normalize import aware, finite


@dataclass(frozen=True)
class SocSettings:
    capacity_kwh: float
    maximum_power_kw: float
    max_age_seconds: float
    max_disagreement_fraction: float = 0.05
    jump_tolerance_fraction: float = 0.02


def validate_soc(
    value: float,
    unit: str,
    measured_at: datetime,
    now: datetime,
    settings: SocSettings,
    *,
    previous: tuple[datetime, float] | None = None,
    bms_kwh: float | None = None,
) -> float:
    """Return SOC energy only for fresh, physically plausible measurements."""
    capacity = finite(settings.capacity_kwh, "capacity")
    power = finite(settings.maximum_power_kw, "battery power")
    age_limit = finite(settings.max_age_seconds, "SOC age limit")
    disagreement = finite(settings.max_disagreement_fraction, "SOC disagreement")
    tolerance = finite(settings.jump_tolerance_fraction, "SOC jump tolerance")
    if (
        capacity <= 0
        or power <= 0
        or age_limit <= 0
        or disagreement < 0
        or tolerance < 0
    ):
        raise InputError("invalid SOC validation settings")
    aware(measured_at, "SOC timestamp")
    aware(now, "now")
    age = (now - measured_at).total_seconds()
    if age < 0 or age > age_limit:
        raise InputError("stale or future SOC measurement")
    raw = finite(value, "SOC")
    if unit == "%":
        energy = raw * capacity / 100
    elif unit == "fraction":
        energy = raw * capacity
    elif unit == "kWh":
        energy = raw
    else:
        raise InputError("unsupported SOC unit")
    if energy < 0 or energy > capacity:
        raise InputError("SOC out of range")
    if previous is not None:
        prior_time, prior_energy = previous
        elapsed = (
            measured_at - aware(prior_time, "previous SOC timestamp")
        ).total_seconds() / 3600
        if (
            elapsed <= 0
            or abs(energy - finite(prior_energy, "previous SOC"))
            > power * elapsed + capacity * tolerance
        ):
            raise InputError("SOC measurement jump")
    if (
        bms_kwh is not None
        and abs(energy - finite(bms_kwh, "BMS SOC")) > capacity * disagreement
    ):
        raise InputError("SOC and BMS disagreement")
    return energy


class SocTracker:
    """Keep the last valid SOC and require a fresh plausible reading after rejection."""

    def __init__(self, settings: SocSettings) -> None:
        self.settings = settings
        self.last_valid: tuple[datetime, float] | None = None
        self.last_rejected_at: datetime | None = None

    def accept(
        self,
        value: float,
        unit: str,
        measured_at: datetime,
        now: datetime,
        *,
        bms_kwh: float | None = None,
    ) -> float:
        """Validate a new reading before replacing the trusted SOC."""
        if self.last_rejected_at is not None and measured_at <= self.last_rejected_at:
            raise InputError("new SOC reading required after rejection")
        try:
            energy = validate_soc(
                value,
                unit,
                measured_at,
                now,
                self.settings,
                previous=self.last_valid,
                bms_kwh=bms_kwh,
            )
        except InputError:
            self.last_rejected_at = measured_at
            raise
        self.last_valid = (measured_at, energy)
        self.last_rejected_at = None
        return energy
