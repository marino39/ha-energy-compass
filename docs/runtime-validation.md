# Solver runtime validation

The supported validation target is the official Raspberry Pi 5 Home Assistant Core 2026.9.1 image, `ghcr.io/home-assistant/raspberrypi5-64-homeassistant:2026.9.1` at digest `sha256:3637812af79ccc020ad3c61787fedaae14dc89918b1ddfc9e96b8f1077b35943`. The checked image reports `aarch64`, Python 3.14.6, NumPy 2.3.2, SciPy 1.18.1, and Home Assistant 2026.9.1. The project requires Python 3.14.2 or newer and pins SciPy 1.18.1 to match this Core image.

The matched fixture package is `pytest-homeassistant-custom-component==0.13.364`; its published dependency metadata requires `homeassistant==2026.9.1`, NumPy 2.3.2, and pytest 9.0.3. The disposable test image installs those exact test dependencies. Home Assistant's extra wheel index is disabled for this install because it contains an older `pytest-asyncio` release and otherwise prevents resolution of the helper's `pytest-asyncio==1.4.0` requirement. The image keeps `boto3` and `botocore` at Core's 1.42.97 versions so the installed `aiobotocore` constraint remains satisfied. The project pytest configuration enables automatic asyncio fixture handling required by the HA fixture package.

From the repository root, with Docker available:

```sh
docker build -f Dockerfile.test -t ha-energy-compass-test:2026.9.1 .
docker run --rm -e PYTEST_ADDOPTS='-p no:cacheprovider' -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m pytest tests/test_runtime.py -q
docker run --rm ha-energy-compass-test:2026.9.1 -m pip check
```

The read-only repository mount and disabled pytest cache keep generated files out of the worktree. The same image can run later test selections by replacing `tests/test_runtime.py` in the second command.

Validation result: the image build completed, `tests/test_runtime.py` passed (1 test), and `pip check` reported no broken requirements. The test run emitted a `SyntaxWarning` from the base image's `rich` package; it did not affect collection or the solver result.

The tiny mixed-integer program maximizes a binary variable under bounds 0–1 with a two-second solver limit. The isolated wheel installation and test passed on the target aarch64 Python runtime. This establishes an importable solver and functioning HiGHS MILP interface; the advisory workload benchmark follows below. No dependency was installed on the live Home Assistant instance.

## Advisory optimizer benchmark

The deterministic `benchmarks.optimizer` workload varies buy and sell prices, solar generation and household load across quarter-hour slots, with a 13.5 kWh battery and finite grid and inverter limits. These measurements used the matched aarch64 image on a Mac host. They measure that container and host combination, not Raspberry Pi 5 hardware. Peak RSS is the process high-water mark, including Python and SciPy startup.

```sh
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 192 --time-limit 10
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 10
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.optimizer --slots 384 --time-limit 0.000001
```

| Slots | Time limit | Result | Solve call elapsed | Peak RSS |
| ---: | ---: | --- | ---: | ---: |
| 192 | 10 s | Optimal, 192 flows | 0.0346 s | 99,628 KiB |
| 384 | 10 s | Optimal, 384 flows | 0.0726 s | 107,052 KiB |
| 384 | 0.000001 s | `SolveError(reason="timeout")` | 0.0149 s | 100,764 KiB |

The timeout case shows version 0.1 rejects a time-limited incumbent instead of returning an unproven plan. All three commands exited successfully; the script reports typed solver outcomes as JSON. The 192-slot case is the two-day quarter-hour representative horizon, and 384 slots stress a four-day horizon. These results should be repeated on actual Raspberry Pi 5 hardware before setting operational time and memory limits.

## Incremental consumption benchmark

`benchmarks.consumption` solves a base plan and then makes serial full-horizon finite-difference probes. The default case uses 96 quarter-hour source slots, a 24-hour display/reference horizon, hourly display intervals, and a 1 kWh probe. The nondefault case uses 144 quarter-hour source slots, a 12-hour display horizon, 36-hour reference horizon, half-hour display intervals, a 0.5 kWh probe, and 20th/80th percentile thresholds. Both use the default 2-second per-probe cap and give the outlook the remainder of a 60-second pipeline allowance after the base solve.

```sh
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.consumption --scenario default
docker run --rm -v "$PWD":/workspace:ro ha-energy-compass-test:2026.9.1 -m benchmarks.consumption --scenario nondefault
```

| Scenario | Base solve | Total elapsed | Reference coverage | Display costs | Peak RSS |
| --- | ---: | ---: | --- | ---: | ---: |
| Default | 0.0145 s | 0.3211 s | Complete, percentile mode | 24/24 known | 96,204 KiB |
| Nondefault | 0.0198 s | 1.3664 s | Complete, percentile mode | 24/24 known | 98,728 KiB |

Both commands exited successfully in the matched ARM64 container on the Mac development host. These timings are not physical Raspberry Pi 5 measurements. The nondefault run probes all 36 reference hours even though it displays only 12 hours. Actual Pi 5 timing remains to be measured before any control use.
