"""Strategy bundles: settings-owned flag overrides and Problem weight resolution.

Pure module, no Home Assistant imports.
"""

from collections.abc import Collection, Mapping

from .models import Strategy

STRATEGY_OWNED_KEYS: tuple[str, ...] = (
    "limit_export_to_pv",
    "limit_grid_charge_price",
    "maximum_grid_charge_price",
    "minimum_export_episode_benefit",
    "autonomy_reserve",
)

# Only settings-owned flags live here; numeric weights come from strategy_weights().
STRATEGY_OVERRIDES: dict[Strategy, dict[str, object]] = {
    "cost_min": {},
    "self_sufficiency": {"autonomy_reserve": True},
    "backup_ready": {"autonomy_reserve": True},
    "pv_swap": {
        "autonomy_reserve": True,
        "limit_export_to_pv": True,
        "limit_grid_charge_price": False,
    },
    "max_export": {
        "autonomy_reserve": False,
        "limit_export_to_pv": False,
        "limit_grid_charge_price": False,
        "minimum_export_episode_benefit": 0.0,
    },
    "grid_friendly": {"autonomy_reserve": False},
}


def resolve_flags(
    strategy: Strategy, values: Mapping[str, object], explicit: Collection[str]
) -> dict[str, object]:
    """Return only the strategy-owned keys this bundle changes, skipping explicit ones."""
    return {
        key: value
        for key, value in STRATEGY_OVERRIDES[strategy].items()
        if key not in explicit
    }


def strategy_weights(
    strategy: Strategy,
    values: Mapping[str, object],
    *,
    max_abs_buy_per_kwh: float,
    site_import_kw: float,
    site_export_kw: float,
) -> dict[str, float | None]:
    """Return the Problem weight fields for one strategy. cost_min returns exact defaults."""
    weights: dict[str, float | None] = {
        "import_weight": 1.0,
        "export_weight": 1.0,
        "import_kwh_weight": 0.0,
        "battery_export_penalty_per_kwh": 0.0,
        "pv_export_margin": 0.0,
        "peak_import_weight": 0.0,
        "cap_violation_weight": 0.0,
        "soft_import_cap_kw": None,
        "soft_export_cap_kw": None,
    }
    if strategy == "self_sufficiency":
        weights["import_kwh_weight"] = max(
            values["self_sufficiency_import_price_per_kwh"],
            max_abs_buy_per_kwh + 0.50,
        )
        weights["battery_export_penalty_per_kwh"] = values[
            "self_sufficiency_export_penalty_per_kwh"
        ]
    elif strategy == "pv_swap":
        weights["pv_export_margin"] = values["pv_swap_margin_per_kwh"]
    elif strategy == "grid_friendly":
        weights["peak_import_weight"] = values["peak_import_price_per_kw"]
        weights["cap_violation_weight"] = values["cap_violation_price_per_kwh"]
        weights["soft_import_cap_kw"] = (
            values["grid_friendly_import_cap_kw"] or site_import_kw
        )
        weights["soft_export_cap_kw"] = (
            values["grid_friendly_export_cap_kw"] or site_export_kw
        )
    return weights
