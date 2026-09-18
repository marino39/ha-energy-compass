# Actual operating mode dwell implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Eliminate short named battery modes and the reported CHARGE_GRID → HOLD → DISCHARGE_GRID → CHARGE_GRID sequence within 90 minutes under a 60-minute minimum.

**Architecture:** Bind one-hot named modes to real flows in the existing MILP, then impose elapsed-time dwell on those modes. Use a configurable minimum active power to prevent numerical near-zero flows from padding a mode. Persist verified named-mode commitments, with a narrowly defined observed-SoC safety stop.

**Tech stack:** Home Assistant 2026.9.1, Python 3.14, SciPy 1.18.1, native config/options flows.

**Spec:** The inline contract below records the user's original minimum-one-hour-per-mode requirement, the reported deployed failure, and the defaults stated during diagnosis.

## Global Constraints

- Default minimum mode duration remains 60 minutes; zero disables mode-duration and minimum-active-power restrictions.
- Add configurable `minimum_mode_power_kw`, default 0.1 kW, finite range 0.001–1000 kW, under Planning. The stated default permits variable power above this floor. Do not add a constant-power restriction.
- Native source and settlement intervals stay unchanged. Use actual UTC elapsed time, including partial first slots and DST.
- Constrain real named states; never extend colors, override labels, or allow hidden zero-power direction timers to satisfy dwell.
- Preserve all existing energy balance, SoC, power, tariff, wear, terminal, throughput and Sell only PV daily-budget constraints. No dependencies, entity ID changes, device writes or notifications.
- Hardware limits always remain hard. Forecast planning must choose sustainable modes and power; the solver must not choose an early SoC safety exit as an arbitrage shortcut.
- Safety HOLD is permitted only when current observed initial SoC is already at the relevant bound and an unexpired persisted active-mode commitment exists. Preserve that commitment and expiry through safety HOLD; no other active mode may start early. This exceptional HOLD may be shorter than 60 minutes and must be exposed as an exception.
- Under enabled strict policy, CURTAIL requires material curtailment and zero battery charge/discharge. This conservative restriction prevents curtailment from masking mode changes. Document the restriction. Duration zero retains prior simultaneous-curtailment behavior.
- No private entity IDs, deployment snapshots or household data in tracked files. Parent owns private replay/deployment artifacts. No push, merge, release, version bump or HA mutation from the implementer.

## Inline specification

For slot duration `h` hours, let `activity = minimum_mode_power_kw*h` and `surplus=max(slot.pv_kwh-slot.load_kwh,0)`. Keep existing physical constraints and bind each selected mode to:

| Mode | Required actual flows |
| --- | --- |
| CHARGE_GRID | `charge >= surplus+activity`; discharge=curtailment=0 |
| CHARGE_PV | `activity <= charge <= surplus`; discharge=curtailment=0 |
| DISCHARGE_GRID | discharge>=activity and grid export>=activity; charge=curtailment=0 |
| SELF_CONSUME | discharge>=activity; charge=grid export=curtailment=0 |
| HOLD | charge=discharge=curtailment=0 |
| CURTAIL | curtailment>=activity; charge=discharge=0 |

Choose exactly one named mode per slot. Use tight physical indicator bounds, including impossible-mode elimination from known slot/capability bounds. For each new mode at start `t`, keep it until at least `t+D`; CHARGE_PV time cannot fund CHARGE_GRID duration, and HOLD cannot start a discharge timer. A fresh active run requires at least D remaining forecast minutes; no invisible extension beyond coverage. A final passive HOLD/CURTAIL run may be clipped by coverage, as may the remaining portion of a pre-existing commitment; never invent future energy.

Independently reconstruct classifications and activity thresholds from flows. Reject mismatches and early transitions. Numerical tolerances must not let a materially idle flow satisfy an active mode. Tiny partial intervals whose activity lies below classifier resolution need explicit handling, not fabricated active labels.

Persist named mode and timezone-aware start time for accepted published advice. Same mode does not restart the clock; changed normal mode starts a new clock. Failed/superseded generations do not mutate the commitment. Restore safely after reload/restart; validate malformed/future persisted data. Legacy `charge`/`discharge` records cannot donate age to a new named mode: preserve any remaining legacy direction guard, then start a fresh named clock from the first valid publication. Keep engine legacy fields only if needed for this bounded compatibility path; do not misrepresent them as the new public policy.

For observed-SoC safety HOLD, keep the original active commitment and deadline. Expose reason, interrupted mode and deadline in `dispatch_policy`; rows truthfully say HOLD. The MILP must not forecast a max-power burst to a bound then use this exception; future modes instead finish their dwell or the plan is infeasible. There is no early unlock on HOLD, changed input, refresh or restart.

### Task 1: Bind minimum duration to actual operating modes end to end

**Read the Global Constraints and Inline specification above; they are this task's requirements.**

**Files owned by implementer:**
- Modify `custom_components/energy_compass/engine/{dispatch_policy,models,normalize,optimize,consumption}.py`.
- Optionally create `engine/machine_state.py` to share the existing classifier without an import cycle; preserve the `consumption.machine_state` public import.
- Modify `custom_components/energy_compass/{settings,runtime,coordinator}.py` for the setting and named commitment lifecycle.
- Modify `strings.json`, `translations/en.json`, `translations/pl.json`, README and `docs/model.md` to describe exact final behavior.
- Add `tests/test_mode_dwell.py`; modify affected dispatch/runtime/options tests only where semantics changed. Preserve their substantive physical assertions; isolated physics/other-feature fixtures may explicitly disable dwell with a written reason.
- No edits to source-management/tariff UI work in sibling worktrees. No unrelated refactor.

**Interfaces:** `Problem` carries minimum power plus named commitment information; `Flow` exposes verified dispatch mode in addition to any retained legacy direction diagnostic. `runtime.build_problem` and `compute` pass/export these fields; coordinator persists only an accepted first current mode or preserves a safety interruption. Existing `machine_state(slot, flow)` remains flow-derived and truthful. Public plan policy identifies actual-mode scope, duration, minimum power and any current safety exception.

- [ ] **Step 1: Write regression tests before production changes.** Test the actual `solve()` results with `machine_state()` and real native HA flows for persistence. Start from a synthetic numeric fixture that reproduces the same failure pattern; parent holds a private captured replay for acceptance. A structural test of hidden binary modes is not sufficient.

  ```python
  # Behavioral assertion for completed active runs, derived from actual flows:
  states = [machine_state(slot, flow) for slot, flow in zip(problem.slots, plan.flows)]
  # Group contiguous equal states with their real slot start/end instants.
  for run in actual_runs(problem.slots, states):
      if run.state in {"CHARGE_GRID", "CHARGE_PV", "DISCHARGE_GRID", "SELF_CONSUME"}:
          assert run.elapsed_minutes >= 60
  ```

  Cover source/sink switches, HOLD pre-credit, actual 100 W floor and a different configured floor, minimum=0 behavior, carried named mode, legacy direction without donated age, partial slots, DST, short horizon tail, all-energy/power bounds, curtail masking, and current measured bound safety versus future planned bound gaming. Add native config/options setting save and coordinator replan/reload tests. Include invalid persisted state and disabled-battery behavior. Tests must exercise full solver behavior, not only the new validator or mocks.

- [ ] **Step 2: Observe RED.** Run focused cases using the pinned image, preserve the relevant output in the task report. Expected failures must demonstrate short actual states or absent runtime behavior; fix fixture/syntax errors first.

  ```bash
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-mode-dwell:/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q tests/test_mode_dwell.py --tb=short --show-capture=no
  ```

- [ ] **Step 3: Implement physical named modes and elapsed dwell.** Gate the table's rows using per-variable physical upper bounds; don't use arbitrary huge M constants. For each mode m and start index i, later j whose start is before i+D must obey `x[j,m] >= x[i,m]-x[i-1,m]`, with explicit initial commitment handling. A legacy direction guard is not the named-mode clock. Preserve total physical vector validation when extra variables are added.

  ```python
  # Core dwell inequality for a transition in a one-hot mode vector:
  model.constrain({current: 1, previous: -1, following: -1}, -np.inf, 0)
  # First uncommitted slot starts its actual chosen mode, including HOLD.
  # Disable a new active transition if coverage_end < start + dwell.
  ```

  Keep all new logic behind `battery is not None and minimum_mode_minutes > 0` for optimizer restrictions. Independently validate each chosen mode against physical flows, activity floor, transitions, coverage and safety-prefix constraints. Report infeasible/timeout honestly; do not fall back to the old weaker solver or silently increase solve budgets.

- [ ] **Step 4: Wire settings, safe persistence and truthful diagnostics.** Use existing native settings metadata for the power floor. Validate and restore commitments, preserve the remaining legacy direction without donated named-mode age, and persist named mode clocks through restart/reload. Compute safety HOLD solely from the current observation and carried commitment, not an optimizer-selectable future SoC event. Keep original lock until its deadline; expose this exception. Update EN/PL copy and docs from direction-hold to the exact implemented named-mode rule.

- [ ] **Step 5: Verify focused cases, discriminating regression, then full CI-equivalent checks once.** Parent will benchmark the captured 49-slot plan and a 192-slot/probe case; cooperate on any performance issue without weakening the contract. Use isolated old-production/current-tests replay for decisive numeric regressions; current checkout must stay restored byte-for-byte afterward.

  ```bash
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-mode-dwell:/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q --tb=short --show-capture=no
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-mode-dwell:/workspace:ro ha-energy-compass-test:2026.9.1 -m ruff check --no-cache .
  docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-mode-dwell:/workspace:ro ha-energy-compass-test:2026.9.1 -m ruff format --check --no-cache .
  ```

- [ ] **Step 6: Commit and report.** Commit only owned task files, report RED/GREEN evidence, exact checks and remaining concerns. Do not spawn reviewers or any agents; controller owns both task and final reviews and the release/deployment sequence.
