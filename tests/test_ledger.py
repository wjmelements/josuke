"""josuke.schema.json must accept every ledger josuke writes, at each step of
`init → add → deploy → accept`, and nothing malformed."""

import copy
import json
import pathlib

import pytest
from click.testing import CliRunner

from josuke.cli import main
from josuke.ledger import validate_ledger

REPO = pathlib.Path(__file__).parent.parent
PROXY = "0x2222222222222222222222222222222222222222"
FACET = {"address": "0x" + "a1" * 20, "codeHash": "0x" + "11" * 32, "initcodeHash": "0x" + "22" * 32}
STATE = {
    "gitCommit": "c" * 40,
    "facets": {"src/A.sol:A": FACET},
    "selectors": {"address": "0x" + "5e" * 20},
    "migration": {"address": "0x" + "d0" * 20},
}


def _entry(history=None):
    entry = {"address": PROXY, "facetSrc": ["src/*.sol"]}
    if history is not None:
        entry["deployments"] = {"314": history}
    return entry


def test_example_ledger_is_valid():
    assert validate_ledger(json.loads((REPO / "josuke.example.json").read_text())) == []


def test_ledgers_from_init_and_add_are_valid(tmp_path):
    runner = CliRunner()
    path = tmp_path / "josuke.json"
    assert runner.invoke(main, ["init", "-f", str(path)]).exit_code == 0
    assert validate_ledger(json.loads(path.read_text())) == []
    assert runner.invoke(main, ["add", PROXY, "src/*.sol", "-f", str(path)]).exit_code == 0
    assert validate_ledger(json.loads(path.read_text())) == []


@pytest.mark.parametrize(
    "history",
    [
        {"proposed": STATE},  # first deploy, before accept
        {"current": STATE},  # accepted
        {"current": STATE, "proposed": {**STATE, "gitCommit": "d" * 40}},  # upgrade pending
        {},  # deploy found everything already routed and dropped `proposed`
    ],
    ids=["first-deploy", "accepted", "pending", "empty"],
)
def test_lifecycle_states_are_valid(history):
    assert validate_ledger([_entry(history)]) == []


def test_errors_are_located_by_json_path():
    bad = _entry({"current": copy.deepcopy(STATE)})
    bad["deployments"]["314"]["current"]["gitCommit"] = "abc"
    del bad["deployments"]["314"]["current"]["facets"]["src/A.sol:A"]["codeHash"]
    errors = validate_ledger([bad])
    assert any(e.startswith("$[0].deployments.314.current.gitCommit:") for e in errors)
    assert any(e.startswith("$[0].deployments.314.current.facets.src/A.sol:A:") and "codeHash" in e for e in errors)


def test_rejects_unknown_fields_and_globbed_source_ids():
    entry = _entry({"current": {**STATE, "facets": {"src/*.sol:A": FACET}}})
    entry["extra"] = True
    errors = validate_ledger([entry])
    assert any("extra" in e for e in errors)
    assert any("src/*.sol:A" in e for e in errors)
