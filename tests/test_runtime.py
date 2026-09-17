"""Check the solver dependency's supported mixed-integer interface."""

from scipy.optimize import Bounds, milp


def test_milp_runtime() -> None:
    result = milp(
        c=[-1.0],
        integrality=[1],
        bounds=Bounds([0], [1]),
        options={"time_limit": 2.0},
    )

    assert result.success
    assert result.x is not None
    assert result.x[0] == 1.0
