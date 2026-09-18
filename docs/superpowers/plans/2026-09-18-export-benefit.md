# Minimum additional export benefit implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Suppress economically trivial additional battery-export periods using a configurable benefit hurdle, default 1 currency unit.

**Architecture:** The existing MILP minimizes physical economic cost plus a decision reserve for each new physical battery-export period. Keep reported physical costs and consumption-probe differences separate from this reserve. Persist current export continuity independently of duration policy so recalculation does not charge the same start repeatedly.

**Tech Stack:** Python 3.14, Home Assistant 2026.9.1, SciPy 1.18.1 MILP, existing pinned test image.

**Spec:** Inline requirements below. User explicitly approved the previously proposed configurable profit threshold. The optional default question received no answer after more than 60 seconds; controller stated a provisional 1 PLN default and the exact additional-export-period semantics. Existing push/merge/release/HA deployment authorization persists. No new approval gate is required.

## Global Constraints

- Setting `minimum_export_episode_benefit`: finite 0..1000 in configured currency, Planning group, default 1; 0 disables the benefit policy. Engine and native defaults agree.
- An export period is consecutive physical battery discharge with simultaneous grid export. It is not a provenance-tracked, closed recharge cycle, a realized-profit guarantee, or a kWh spread.
- Let `C = grid_cost + wear_cost - terminal_credit`, `P = minimum_export_episode_benefit`, and `N` be new export-period starts. Optimize `C + P*N`. Thus any feasible alternative with k fewer starts must worsen C by at least P*k within solver numerical tolerance. Use zero relative MIP gap when enabled, within existing deadlines; never accept an unproved time-limited incumbent.
- Keep `Plan.objective` and grid/wear/terminal reporting physical. Decision reserve is separate and must not appear as electricity expenditure. Consumption probes retain physical objective differences under the same policy.
- Count starts exactly, including when minimum mode duration is 0. Curtailment, hidden binary direction and idle slots cannot hide physical export. With duration >0 reuse the physical `mode_DISCHARGE_GRID` indicator.
- With duration 0 and benefit >0, enforce only a material battery-export detector: both discharge and grid export must meet `minimum_mode_power_kw * slot_hours` when active. This extends the existing floor to export-profit accounting only, without enabling all six modes or restricting standalone household discharge, PV export, or simultaneous curtailment/charging. Document this explicit interaction.
- Continue using native settlement slots and UTC elapsed durations. Preserve all existing SoC, physical power, source prices, wear, terminal, daily throughput, daily PV export budgets, duration locks and safety HOLD behavior.
- A valid ongoing current export period is not charged again. A future planned period is not paid in advance. Old direction/mode records alone do not prove export continuity. Persist only accepted published current advice, separately from dwell persistence.
- No dependency changes, new entities, inverter writes, household data in git, tariff multiplier edits, or unrelated configuration work. Keep code in the current isolated worktree; do not edit siblings. Parent owns version bump, release, deployment and private replays.

### Task 1: End-to-end configurable export-benefit hurdle

**Files:** Create `custom_components/energy_compass/engine/export_benefit.py` and `tests/test_export_benefit.py`. Modify `engine/models.py`, `engine/normalize.py`, `engine/optimize.py`, `settings.py`, `runtime.py`, `coordinator.py`, `strings.json`, `translations/en.json`, `translations/pl.json`, README and `docs/model.md`. Extend focused existing tests only where old fixtures intentionally isolate duration/physical behavior; retain their physical assertions. Do not globally disable the new feature to make the suite pass.

**Interfaces and exact semantics:**

- Add `Problem.minimum_export_episode_benefit: float = 1.0` and `Problem.initial_export_active: bool = False` as trailing default fields. Validate numeric bounds and strict bool, including battery-disabled configurations.
- Append backwards-compatible defaulted diagnostics to `Plan`: `new_export_episodes: int = 0`, `export_episode_reserve: float = 0.0`. Preserve existing positional constructors and physical `objective`.
- Add engine helper functions to build and independently validate the physical export indicators/start indicators and return the count/reserve. Keep the helper as the sole owner of benefit-policy logic. Vectors must contain all new solver variables so existing full-vector validation remains correct; independently verify new binary integrality, start identities, physical indicator consistency and reserve, not just the solver inequalities.
- With enabled benefit and battery, for slot i use e_i=`mode_DISCHARGE_GRID` if available. Otherwise create binary e,z using finite physical upper bounds Bd,Go and a=`minimum_mode_power_kw*elapsed_hours`:

```python
# conceptual sparse constraints, not new physical energy allocations
bd <= Bd * (e + z)
gout <= Go * (e + 1 - z)
bd >= a * e
gout >= a * e
```

  Set e upper bound 0 if a is at/below the existing classifier resolution 2e-6 kWh, or physically unavailable. e=0 requires bd=0 OR gout=0; e=1 requires both flows to be material. This prevents a CURTAIL label from hiding discharge/export. No fake battery_grid allocation may substitute for physical bd/gout.
- Exact binary start s_i=max(0,e_i-e_previous). At index0, previous is 1 only for `Problem.initial_export_active`; otherwise 0. Use all three bounds, including upper bounds, so a start cannot be invented or omitted:

```python
s >= e - previous
s <= e
s <= 1 - previous
```

  Give s objective coefficient P. All variables and independent validation use the same prior. Reserve=P*sum(s). Physical Plan.objective still computes C. Benefit-disabled or battery-free cases report zero reserve/count and preserve prior optimization semantics.
- Extend `_Model` only as needed to request `mip_rel_gap=0` when benefit policy is active. Numerical solver tolerances remain documented. Preserve solver timeout/infeasible failure handling.
- Runtime propagates the setting, and exposes `minimum_export_episode_benefit`, `new_export_episodes`, `export_episode_reserve`, `export_benefit_scope: additional_battery_export_period` in dispatch policy. Attribute naming must make clear this is a decision reserve over the solved horizon, not a charge on the bill.
- Use a separate native HA Store for current export continuity, e.g. `energy_compass.<entry_id>.export`, to avoid changing legacy dwell records. Record only `{generated_at, until}` when the first physical row exports from battery; `until` is the end of the contiguous current export period in the accepted plan, not merely the first settlement boundary. A valid record satisfies aware timestamps, generated_at<=now<until and 0<until-generated_at<=48 hours. This bounds stale/future/corrupt state. A record does not waive any dwell/physical rule and expires at the old scheduled period end. It does not contain future starts or fees already paid.
- Pass this optional record to `build_problem`/`compute` via trailing keyword args and set initial_export_active from its validity. Preserve existing callers. Native preview without a live record starts fresh. On accepted results, refresh the current record using physical first-row discharge/export, regardless of duration setting; HOLD/PV-only export clears it. Failed and superseded results never mutate it. Unload flushes it, restart restores it. With no benefit configured yet, legacy records do not grant a free new period. Persisted current continuity may be maintained while threshold0 so subsequently enabling it does not fabricate a new start mid-period.
- For physical continuity recognition use a small existing solver/classifier tolerance; never rely on CURTAIL-precedence machine_state. Do not persist private full forecasts.
- Native Planning selector, helper bindings, validation and EN/PL UI descriptions must include currency (not currency/kWh). Describe incremental full-horizon benefit vs fewer export periods; prices, losses, wear and optional terminal value are already included. Explain 0 off, continuing-period exemption, duration0 floor interaction, and possibility of real low-power export bridging a valley without recharge.

- [ ] **Step 1: Add failing economic solver tests first.** Name the behavior before code. Include a small no-PV initial inventory example with final SoC preserved: one-hour sale followed by one-hour recharge, flat buy0.61, sell0.65, efficiencies1, wear0, battery1kWh/1kW/initial1, export-budget disabled for this isolated fixture. With threshold0 the physical cycle earns0.04; with threshold1 it must disappear. With sell2, threshold1 it must remain; threshold2 removes it. A test pattern is:

```python
low = replace(problem, minimum_export_episode_benefit=0)
assert solve(low).flows[0].discharge_kwh > 0.9
guarded = solve(replace(problem, minimum_export_episode_benefit=1))
assert sum(f.discharge_kwh for f in guarded.flows) < 1e-6
assert guarded.export_episode_reserve == 0
```

  Include near-boundary cases, efficiency/wear opportunity cost, two separated profitable periods each charged once, current-period exemption, UTC partial slots, invalid numeric/bool values, battery-free behavior, no blocked self-consumption or PV-only export, and independent result validation rejecting corrupted start/indicator vectors. Cover profit with duration0; CURTAIL+discharge/export cannot evade, CURTAIL+charging remains possible, zero-flow gaps cannot be fictitious continuous export.
- [ ] **Step 2: Run RED in pinned image.** Command `docker run --rm -v /Users/marcin/Prywatne/ha-recovery/worktrees/energy-compass-mode-dwell:/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest -p no:cacheprovider -q tests/test_export_benefit.py --tb=short --show-capture=no`. Accept failures due to missing behavior/API only, not collection mistakes. Report exact counts and one numeric old-policy failure once APIs exist or with a narrowly isolated old-production discrimination replay.
- [ ] **Step 3: Implement engine helper and propagation.** Add tests for native settings/default migration/helper bounds and accepted-result continuity before implementing native persistence. Run RED for those too. Include save/reload unchanged-current-period, expired/future/corrupt record, future starts not prepaid, threshold changes, duration0 persistence, and failed/superseded results. Use asyncio events, not positive sleep polling under freezer. Tests must use actual solver behavior for economics and physical claims.
- [ ] **Step 4: Preserve probe cost contract and documentation.** A test must demonstrate the decision reserve affects plan choice yet is excluded from Plan.objective and physical probe differences. Document full-horizon incremental semantics, not per-cycle realized cash claims. Any new constraints or accounting deviations must be reported before proceeding.
- [ ] **Step 5: Run focused GREEN, then full suite, Ruff check/format and pip check once.** Existing baseline cc8e843 is the recently verified364-pass release. Do not remove useful assertions; isolate intentional legacy-policy fixtures explicitly. Parent independently replays captured household scenario and192native slots with probes, and runs Hassfest. Budgets remain10s base/5s probe/60s total.
- [ ] **Step 6: Self-review and commit task-owned files.** Detailed report under this plan's ignored SDD workspace includes RED/GREEN commands/results, changed file list, behavioral limitations and any necessary fixture changes. No version bump, push, release or deployment. Do not spawn subagents; parent owns fresh task and whole-branch review.
