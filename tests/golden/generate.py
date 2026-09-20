#!/usr/bin/env python3
"""Print the cost_min golden snapshot as one canonical JSON document to stdout.

Invoked by path (`python tests/golden/generate.py`) because the test container's
entrypoint is bare `python`; the workspace mount is read-only so this script writes
no files. Redirect stdout on the host to regenerate `tests/golden/cost_min.json`.
"""

import hashlib
import json
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fixtures import golden_problems

from custom_components.energy_compass.engine import optimize

_captured: dict[str, object] = {}
_original_solve = optimize._Model.solve


def _spy(self, time_limit_s):
    _captured["cost"] = list(self.cost)
    _captured["lower"] = list(self.lower)
    _captured["upper"] = list(self.upper)
    _captured["integrality"] = list(self.integrality)
    _captured["rows"] = list(self.rows)
    return _original_solve(self, time_limit_s)


def _render_bound(value: float) -> float | str:
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    return value


def _rows_sha256(rows: list[tuple[dict[int, float], float, float]]) -> str:
    payload = [
        [sorted(terms.items()), _render_bound(lo), _render_bound(hi)]
        for terms, lo, hi in rows
    ]
    digest = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(digest).hexdigest()


def _snapshot_one(problem) -> dict:
    _captured.clear()
    plan = optimize.solve(problem)
    return {
        "cost": _captured["cost"],
        "lower": _captured["lower"],
        "upper": [_render_bound(value) for value in _captured["upper"]],
        "integrality": _captured["integrality"],
        "rows_sha256": _rows_sha256(_captured["rows"]),
        "flows": [
            [getattr(flow, field.name) for field in fields(flow)] for flow in plan.flows
        ],
        "objective": plan.objective,
        "grid_cost": plan.grid_cost,
        "wear_cost": plan.wear_cost,
        "terminal_credit": plan.terminal_credit,
        "new_export_episodes": plan.new_export_episodes,
        "export_episode_reserve": plan.export_episode_reserve,
    }


def main() -> None:
    optimize._Model.solve = _spy
    try:
        snapshot = {
            name: _snapshot_one(problem) for name, problem in golden_problems().items()
        }
    finally:
        optimize._Model.solve = _original_solve
    print(json.dumps(snapshot, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
