"""Tests for `josuke check`, run end to end against the committed check-project
Foundry fixture (skipped without `forge`)."""

import json
import pathlib
import shutil

import pytest
from click.testing import CliRunner

from josuke.cli import main
from josuke.deploy import facet_from_source_id, facet_initcode, keccak_hex

pytestmark = pytest.mark.skipif(shutil.which("forge") is None, reason="requires the `forge` binary")

ROOT = pathlib.Path(__file__).parent / "fixtures" / "check-project"
PROXY = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
COMMIT = "c" * 40
OWNABLE = "src/facets/Ownable.sol:Ownable"
COUNTER = "src/facets/Counter.sol:Counter"
CONFIGURED = "src/Configured.sol:Configured"
ARGS = {"limits": {"admin": "0x" + "ad" * 20, "level": 3}}


@pytest.fixture(autouse=True)
def _in_fixture(monkeypatch):
    monkeypatch.chdir(ROOT)


def _record(source_id, address, args=None):
    initcode, _ = facet_initcode(facet_from_source_id(source_id), ROOT, args, prompt=False)
    record = {"address": address, "codeHash": "0x" + "00" * 32, "initcodeHash": keccak_hex(initcode)}
    if args:
        record["constructorArgs"] = args
    return record


def _entry(facet_src, address=PROXY, **history):
    entry = {"address": address, "facetSrc": facet_src}
    if history:
        entry["deployments"] = {"314": {k: {"gitCommit": COMMIT, "facets": v} for k, v in history.items()}}
    return entry


def _check(tmp_path, ledger, *args):
    path = tmp_path / "josuke.json"
    path.write_text(json.dumps(ledger))
    return CliRunner().invoke(main, ["check", "-f", str(path), *args])


def test_first_deploy_lists_every_facet_as_new(tmp_path):
    result = _check(tmp_path, [_entry(["src/facets/*.sol"])])
    assert result.exit_code == 0, result.output
    assert "nothing deployed yet" in result.output
    assert f"NEW        {COUNTER}" in result.output
    assert f"NEW        {OWNABLE}" in result.output
    assert "would deploy 2 facets" in result.output
    assert "generated · returns 5 selectors" in result.output  # 4 facet selectors + selectors()


def test_selector_exported_by_two_facets_fails(tmp_path):
    result = _check(tmp_path, [_entry(["src/facets/*.sol", "src/Clash.sol:Clash"])])
    assert result.exit_code == 1
    assert f"is exported by both {OWNABLE} and src/Clash.sol:Clash" in result.output
    assert "owner()" in result.output


def test_identical_bytecode_shares_one_deployment_so_no_clash(tmp_path):
    result = _check(tmp_path, [_entry(["src/Clash.sol:Clash", "src/Clash.sol:ClashTwin"])])
    assert result.exit_code == 0, result.output
    assert "would deploy 1 facet" in result.output


def test_identical_bytecode_at_two_recorded_addresses_clashes(tmp_path):
    # deploy keeps each installed record, so the twins stay at distinct addresses.
    current = {
        "src/Clash.sol:Clash": _record("src/Clash.sol:Clash", "0x" + "a1" * 20),
        "src/Clash.sol:ClashTwin": _record("src/Clash.sol:ClashTwin", "0x" + "a2" * 20),
    }
    result = _check(tmp_path, [_entry(["src/Clash.sol:Clash", "src/Clash.sol:ClashTwin"], current=current)])
    assert result.exit_code == 1
    assert "is exported by both src/Clash.sol:Clash and src/Clash.sol:ClashTwin" in result.output


def test_unmatched_glob_fails(tmp_path):
    result = _check(tmp_path, [_entry(["src/nowhere/*.sol"])])
    assert result.exit_code == 1
    assert "src/nowhere/*.sol matched no deployable contracts" in result.output


def test_explicit_interface_has_no_creation_code(tmp_path):
    result = _check(tmp_path, [_entry(["src/Clash.sol:IClash"])])
    assert result.exit_code == 1
    assert "src/Clash.sol:IClash: no creation code" in result.output


def test_missing_contract_fails(tmp_path):
    result = _check(tmp_path, [_entry(["src/Clash.sol:Gone"])])
    assert result.exit_code == 1
    assert "src/Clash.sol:Gone: cannot read its ABI" in result.output


def test_missing_constructor_args_are_reported_not_blocking(tmp_path):
    result = _check(tmp_path, [_entry([CONFIGURED])])
    assert result.exit_code == 0, result.output
    assert "deploy asks for limits" in result.output
    assert "asking for constructor args of 1" in result.output


def test_struct_constructor_args_encode_and_match(tmp_path):
    current = {CONFIGURED: _record(CONFIGURED, "0x" + "c0" * 20, ARGS)}
    result = _check(tmp_path, [_entry([CONFIGURED], current=current)])
    assert result.exit_code == 0, result.output
    assert f"UNCHANGED  {CONFIGURED}" in result.output
    assert "HEAD matches current ccccccc" in result.output


def test_recorded_args_that_do_not_encode_fail(tmp_path):
    record = _record(CONFIGURED, "0x" + "c0" * 20, ARGS)
    record["constructorArgs"] = {"limits": {**ARGS["limits"], "level": 256}}
    result = _check(tmp_path, [_entry([CONFIGURED], current={CONFIGURED: record})])
    assert result.exit_code == 1
    assert f"{CONFIGURED}: recorded constructorArgs do not encode" in result.output


def test_plan_against_current_and_strict_drift(tmp_path, monkeypatch):
    monkeypatch.setattr("josuke.check.Baselines.declarations", lambda self, commit, source_ids: [])  # no git history
    current = {
        OWNABLE: _record(OWNABLE, "0x" + "0a" * 20),
        COUNTER: {**_record(COUNTER, "0x" + "0c" * 20), "initcodeHash": "0x" + "ee" * 32},  # stale
        "src/Old.sol:Old": _record(OWNABLE, "0x" + "0d" * 20),
    }
    ledger = [_entry(["src/facets/*.sol"], current=current)]

    result = _check(tmp_path, ledger)
    assert result.exit_code == 0, result.output
    assert f"UNCHANGED  {OWNABLE}" in result.output
    assert f"CHANGED    {COUNTER}" in result.output
    assert "REMOVED    src/Old.sol:Old" in result.output
    assert f"HEAD differs from current cccccc" in result.output
    assert "blocking with --strict" in result.output

    strict = _check(tmp_path, ledger, "--strict")
    assert strict.exit_code == 1
    assert f"changes {COUNTER}; drops src/Old.sol:Old" in strict.output


def test_strict_compares_with_proposed_when_one_is_staged(tmp_path):
    staged = {OWNABLE: _record(OWNABLE, "0x" + "0a" * 20), COUNTER: _record(COUNTER, "0x" + "0c" * 20)}
    ledger = [_entry(["src/facets/*.sol"], current={OWNABLE: staged[OWNABLE]}, proposed=staged)]
    result = _check(tmp_path, ledger, "--strict")
    assert result.exit_code == 0, result.output
    assert f"NEW        {COUNTER}" in result.output  # still new against current
    assert "HEAD matches proposed" in result.output


def test_schema_violation_stops_the_check(tmp_path):
    result = _check(tmp_path, [{"address": PROXY, "facetSrc": []}])
    assert result.exit_code == 1
    assert "schema: $[0].facetSrc:" in result.output
    assert "build " not in result.output


def test_duplicate_proxy_fails(tmp_path):
    ledger = [_entry(["src/facets/*.sol"]), _entry([COUNTER], address=PROXY.lower())]
    result = _check(tmp_path, ledger)
    assert result.exit_code == 1
    assert f"{PROXY} is listed 2 times" in result.output


def test_chain_option_checks_a_chain_with_nothing_recorded(tmp_path):
    ledger = [_entry(["src/facets/*.sol"], current={OWNABLE: _record(OWNABLE, "0x" + "0a" * 20)})]
    result = _check(tmp_path, ledger, "--chain", "31415926")
    assert result.exit_code == 0, result.output
    assert "chain 31415926  ·  nothing current" in result.output
    assert f"NEW        {OWNABLE}" in result.output


def test_markdown_wraps_the_report(tmp_path):
    result = _check(tmp_path, [_entry(["src/facets/*.sol", "src/Clash.sol:Clash"])], "--format", "markdown")
    assert result.exit_code == 1
    assert result.output.startswith(f"### josuke check `{tmp_path / 'josuke.json'}`: failed, 1 finding\n")
    assert "```text\n" in result.output


def test_struct_selectors_match_solc():
    from josuke.deploy import facet_selectors
    from josuke.proc import run

    identifiers = json.loads(run(["forge", "inspect", CONFIGURED, "methodIdentifiers", "--json"], ROOT))
    assert {s.selector for s in facet_selectors(facet_from_source_id(CONFIGURED), ROOT)} == {
        "0x" + selector for selector in identifiers.values()
    }


# -- storage -----------------------------------------------------------------


def test_facets_sharing_one_layout_pass(tmp_path):
    result = _check(tmp_path, [_entry(["src/facets/*.sol", "src/Storage.sol:Tally", "src/Storage.sol:TallyToo"])])
    assert result.exit_code == 0, result.output
    assert "storage      4 facets checked, 3 slots shared" in result.output


def test_different_variables_appended_at_one_slot_fail(tmp_path):
    result = _check(tmp_path, [_entry(["src/facets/*.sol", "src/Storage.sol:Tally", "src/Storage.sol:Flag"])])
    assert result.exit_code == 1
    assert (
        f"{PROXY} storage slot 2: src/Storage.sol:Flag declares address flag_ "
        "over src/Storage.sol:Tally's uint64 tally_ at slot 2"
    ) in result.output


def test_facet_ignoring_the_shared_layout_fails(tmp_path):
    result = _check(tmp_path, [_entry([OWNABLE, "src/Storage.sol:Standalone"])])
    assert result.exit_code == 1
    assert f"{PROXY} storage slot 0: src/Storage.sol:Standalone declares uint256 total_" in result.output
    assert f"over {OWNABLE}'s address owner_ at slot 0" in result.output


def test_facets_without_a_layout_are_listed_as_not_visible():
    from josuke.check import Findings, _check_storage

    layout = {
        "storage": [{"label": "owner_", "offset": 0, "slot": "0", "type": "t_address"}],
        "types": {"t_address": {"encoding": "inplace", "label": "address", "numberOfBytes": "20"}},
    }
    findings = Findings()
    line = _check_storage(PROXY, {"a.sol:A": (None, [], layout), "impl.evm": (None, [], None)}, findings)
    assert findings.failures == []
    assert line == "  storage      1 facet checked, 0 slots shared; not visible: impl.evm"


def test_packed_variables_overlapping_at_different_offsets_fail():
    from josuke.check import Findings, _check_storage

    types = {
        "t_uint64": {"encoding": "inplace", "label": "uint64", "numberOfBytes": "8"},
        "t_uint128": {"encoding": "inplace", "label": "uint128", "numberOfBytes": "16"},
    }
    packed = {"storage": [
        {"label": "a", "offset": 0, "slot": "3", "type": "t_uint64"},
        {"label": "b", "offset": 8, "slot": "3", "type": "t_uint64"},
    ], "types": types}
    wide = {"storage": [{"label": "c", "offset": 0, "slot": "3", "type": "t_uint128"}], "types": types}
    findings = Findings()
    _check_storage(PROXY, {"p.sol:P": (None, [], packed), "w.sol:W": (None, [], wide)}, findings)
    assert len(findings.failures) == 2
    assert any("storage slot 3 offset 8: p.sol:P declares uint64 b over w.sol:W's uint128 c at slot 3" in f
               for f in findings.failures)
