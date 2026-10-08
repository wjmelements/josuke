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
    # COMMIT isn't in git; test_check_history.py covers recorded storage.
    monkeypatch.setattr("josuke.check.Baselines.declarations", lambda self, commit, source_ids: [])


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


def test_colliding_signatures_fail(tmp_path):
    result = _check(tmp_path, [_entry(["src/Collide.sol:Burn", "src/Collide.sol:Collate"])])
    assert result.exit_code == 1
    assert "selector 0x42966c68 is both burn(uint256) and collate_propagate_storage(bytes16)" in result.output


def test_internal_function_in_storage_warns(tmp_path):
    result = _check(tmp_path, [_entry(["src/Pointers.sol:Pointers"])])
    assert result.exit_code == 0, result.output
    assert "slot 0: function () hook holds an internal function" in result.output
    assert "mapping(uint256 => struct Pointers.Hooks) hooks holds an internal function" in result.output
    assert "callback holds" not in result.output


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


def test_linked_library_names_the_library(tmp_path):
    result = _check(tmp_path, [_entry(["src/Linked.sol:Linked"])])
    assert result.exit_code == 1
    assert (
        f"{PROXY} src/Linked.sol:Linked: links external library src/Linked.sol:Doubler; "
        "josuke does not support linked libraries"
    ) in result.output


def test_missing_contract_fails(tmp_path):
    result = _check(tmp_path, [_entry(["src/Clash.sol:Gone"])])
    assert result.exit_code == 1
    assert "src/Clash.sol:Gone: cannot read its ABI or runtime" in result.output


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


def test_plan_against_current_and_strict_drift(tmp_path):
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


def test_oversize_runtime_fails_even_when_forge_build_succeeds(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text('[profile.default]\nsolc="0.8.28"\n')
    (project / "src/Big.sol").write_text(
        'pragma solidity 0.8.28; contract Big { function data() external pure returns(bytes memory) { return hex"'
        + "ab" * 26000 + '"; }}'
    )
    monkeypatch.chdir(project)
    result = _check(tmp_path, [_entry(["src/Big.sol:Big"])])
    assert result.exit_code == 1, result.output
    assert "src/Big.sol:Big runtime code" in result.output
    assert "exceeds 24576 bytes" in result.output


def test_generated_selectors_runtime_is_size_checked(tmp_path, monkeypatch):
    from josuke.selectors import Selector

    selectors = [Selector(f"0x{i:08x}", f"f{i}()") for i in range(3000)]
    monkeypatch.setattr("josuke.check.facet_selectors", lambda *args: selectors)
    result = _check(tmp_path, [_entry([OWNABLE])])
    assert result.exit_code == 1, result.output
    assert "generated selectors() runtime code" in result.output
    assert "exceeds 24576 bytes" in result.output


@pytest.mark.skipif(shutil.which("make") is None, reason="requires make")
@pytest.mark.parametrize("size", [24576, 24577, None])
def test_evm_runtime_limit_and_missing_artifact_warning(tmp_path, monkeypatch, size):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/Only.evm").write_text("// artifact built by make\n")
    (project / "foundry.toml").write_text('[profile.default]\nsolc="0.8.28"\n')
    artifact = {
        "bytecode": {"object": "0x6000"},
        "abi": [{"type": "function", "name": "only", "inputs": [], "outputs": []}],
    }
    if size is not None:
        artifact["deployedBytecode"] = {"object": "0x" + "00" * size}
    (project / "artifact.json").write_text(json.dumps(artifact))
    (project / "Makefile").write_text('out/Only.evm/Only.json:\n\tmkdir -p $(@D)\n\tcp artifact.json $@\n')
    monkeypatch.chdir(project)
    result = _check(tmp_path, [_entry(["src/Only.evm"])])
    assert result.exit_code == (1 if size == 24577 else 0), result.output
    if size is None:
        assert "runtime code size not checked" in result.output
    elif size == 24577:
        assert "exceeds 24576 bytes" in result.output
    else:
        assert "WARNING" not in result.output


@pytest.mark.parametrize("amount", [-1, 1])
def test_missing_constructor_arg_does_not_hide_incompatible_recorded_arg(tmp_path, monkeypatch, amount):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text('[profile.default]\nsolc="0.8.28"\n')
    (project / "src/A.sol").write_text('pragma solidity 0.8.28; contract A { constructor(uint256 amount, uint256 salt) {} }')
    monkeypatch.chdir(project)
    record = {"address": OTHER, "codeHash": "0x" + "00" * 32, "initcodeHash": "0x" + "00" * 32,
              "constructorArgs": {"amount": amount}}
    result = _check(tmp_path, [_entry(["src/A.sol:A"], current={"src/A.sol:A": record})])
    assert result.exit_code == (1 if amount < 0 else 0), result.output
    assert ("constructorArgs do not encode" if amount < 0 else "deploy asks for salt") in result.output


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
    from josuke.check import Findings, _check_storage, _declarations

    layout = {
        "storage": [{"label": "owner_", "offset": 0, "slot": "0", "type": "t_address"}],
        "types": {"t_address": {"encoding": "inplace", "label": "address", "numberOfBytes": "20"}},
    }
    findings = Findings()
    resolved = {"a.sol:A": (None, [], layout), "impl.evm": (None, [], None)}
    line = _check_storage(PROXY, resolved, _declarations({"a.sol:A": layout}), findings)
    assert findings.failures == []
    assert line == "  storage      1 facet checked, 0 slots shared; not visible: impl.evm"


def test_packed_variables_overlapping_at_different_offsets_fail():
    from josuke.check import Findings, _check_storage, _declarations

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
    layouts = {"p.sol:P": packed, "w.sol:W": wide}
    _check_storage(PROXY, {k: (None, [], v) for k, v in layouts.items()}, _declarations(layouts), findings)
    assert len(findings.failures) == 2
    assert any("storage slot 3 offset 8: p.sol:P declares uint64 b over w.sol:W's uint128 c at slot 3" in f
               for f in findings.failures)
