"""Flow-derived operating states shared by planning and display."""

from .models import Flow, MachineState, Slot

_TOL = 1e-6


def machine_state(slot: Slot, flow: Flow) -> MachineState:
    """Choose one display state by curtail, charge, export, discharge precedence."""
    if flow.curtail_kwh > _TOL:
        return "CURTAIL"
    if flow.charge_kwh > max(slot.pv_kwh - slot.load_kwh, 0) + _TOL:
        return "CHARGE_GRID"
    if flow.charge_kwh > _TOL:
        return "CHARGE_PV"
    if flow.discharge_kwh > _TOL and flow.grid_export_kwh > _TOL:
        return "DISCHARGE_GRID"
    if flow.discharge_kwh > _TOL:
        return "SELF_CONSUME"
    return "HOLD"
