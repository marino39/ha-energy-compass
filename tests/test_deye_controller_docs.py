"""Fail when the Deye controller, its package or dashboard examples drift from the docs.

The guides are mirrors (AGENTS.md, Documentation sweep): every blueprint input,
package entity, runtime code and dashboard example must be named in both.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints/automation/energy_compass/deye_solarman_controller.yaml"
PACKAGE = ROOT / "packages/energy_compass_deye.yaml"
GENERATOR = ROOT / "tools/deye_controller/build.py"
GUIDES = [ROOT / "docs/guide.en.md", ROOT / "docs/guide.pl.md"]
INSTALLATION = ROOT / "docs/installation.md"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))


def blueprint_inputs():
    doc = yaml.load(BLUEPRINT.read_text(), Loader=_Loader)
    return sorted(
        key
        for section in doc["blueprint"]["input"].values()
        for key in section["input"]
    )


def package_entities():
    package = yaml.safe_load(PACKAGE.read_text())
    entities = [
        f"{domain}.{key}"
        for domain in ["input_select", "input_boolean", "input_datetime"]
        for key in package[domain]
    ]
    for block in package["template"]:
        entities += [f"sensor.{sensor['unique_id']}" for sensor in block["sensor"]]
    return sorted(entities)


def runtime_codes():
    text = GENERATOR.read_text()
    codes = set(re.findall(r'get\("code", "([a-z_]+)"\)', text))
    for expression in re.findall(r"code=([^,]{0,120})", text):
        codes |= set(re.findall(r"'([a-z_]+)'", expression))
    return sorted(codes)


def examples():
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "examples/dashboards").glob("*.yaml")
    )


def test_extraction_finds_the_known_surface():
    assert {"plan_entity", "solarman_device", "capacity_kwh", "old_writers"} <= set(
        blueprint_inputs()
    )
    assert "input_select.energy_compass_deye_mode" in package_entities()
    assert "sensor.energy_compass_deye_tou_settings" in package_entities()
    assert {"ok", "blocked", "restored", "waiting", "write_failed"} <= set(
        runtime_codes()
    )
    assert "examples/dashboards/plan_chart.yaml" in examples()


@pytest.mark.parametrize("guide", GUIDES, ids=lambda path: path.name)
@pytest.mark.parametrize(
    "identifier",
    [*blueprint_inputs(), *package_entities(), *runtime_codes()],
)
def test_guides_document_controller_surface(guide, identifier):
    assert f"`{identifier}`" in guide.read_text(), (
        f"{guide.name} does not mention `{identifier}`; follow AGENTS.md Documentation sweep"
    )


@pytest.mark.parametrize("example", examples())
def test_installation_links_every_dashboard_example(example):
    assert f"../{example}" in INSTALLATION.read_text()
