"""The generated dashboard examples: current, placeholder-only, valid templates."""

import importlib.util
from pathlib import Path

import pytest
from homeassistant.helpers.template import Template
from homeassistant.util import yaml as yaml_util

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "dashboards_build", ROOT / "tools/dashboards/build.py"
)
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)
EXAMPLES = sorted((ROOT / "examples/dashboards").glob("*.yaml"))


def test_committed_examples_match_generator():
    stale = [
        path.relative_to(ROOT)
        for path, text in build.outputs().items()
        if not path.exists() or path.read_text() != text
    ]
    assert not stale, "run python tools/dashboards/build.py"
    assert sorted(build.outputs()) == EXAMPLES


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_examples_hold_no_installation_entities(path):
    text = path.read_text()
    for private in ["home_pilot", "inverter_deye", "pl-PL"]:
        assert private not in text


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
async def test_markdown_cards_render(hass, path):
    section = yaml_util.load_yaml(path)
    cards = [card for card in section["cards"] if card["type"] == "markdown"]
    assert cards
    for card in cards:
        assert Template(card["content"], hass).async_render(parse_result=False)


@pytest.mark.parametrize("lang", list(build.LABELS))
def test_every_section_builds_in_every_language(lang):
    for name, section in build.SECTIONS.items():
        assert section(build.ENTITIES, lang)["cards"], name
