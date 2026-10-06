"""`josuke check` against the storage of a recorded deployment: a real git
repository whose Foundry project sits in a subdirectory, as in filecoin-services."""

import json
import shutil

import pytest
from click.testing import CliRunner

from josuke.check import _fit
from josuke.cli import main
from josuke.deploy import facet_from_source_id, facet_initcode, keccak_hex
from josuke.proc import run

pytestmark = pytest.mark.skipif(
    shutil.which("forge") is None or shutil.which("git") is None, reason="requires `forge` and `git`"
)

PROXY = "0x2222222222222222222222222222222222222222"
FOUNDRY_TOML = """[profile.default]
src = "src"
out = "out"
libs = []
solc_version = "0.8.28"
bytecode_hash = "none"
cbor_metadata = false
"""
LAYOUT = """// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
abstract contract Layout {{
{body}
}}
"""
FACET = """// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;
import {{Layout}} from "./Layout.sol";
contract {name} is Layout {{
{body}
}}
"""


def _git(root, *args) -> str:
    return run(["git", *args], root).strip()


def _write(project, layout, facets):
    (project / "src" / "Layout.sol").write_text(LAYOUT.format(body=layout))
    for name, body in facets.items():
        (project / "src" / f"{name}.sol").write_text(FACET.format(name=name, body=body))


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    """Commit and "deploy" a project; returns a function that rewrites its
    sources (uncommitted) and runs `josuke check` against that deployment."""
    repo = tmp_path / "repo"
    project = repo / "contracts"
    (project / "src").mkdir(parents=True)
    (project / "foundry.toml").write_text(FOUNDRY_TOML)
    monkeypatch.chdir(project)

    def deploy(layout, facets):
        _write(project, layout, facets)
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "deployed")
        run(["forge", "build"], project)
        records = {}
        for name in facets:
            source_id = f"src/{name}.sol:{name}"
            initcode, _ = facet_initcode(facet_from_source_id(source_id), project, {}, prompt=False)
            records[source_id] = {
                "address": "0x" + f"{len(records) + 1:02x}" * 20,
                "codeHash": "0x" + "00" * 32,
                "initcodeHash": keccak_hex(initcode),
            }
        commit = _git(repo, "rev-parse", "HEAD")

        def check(layout, facets, recorded=None):
            """`recorded(commit, records)`: the chain's deployments; default all `current`."""
            for path in (project / "src").glob("*.sol"):
                path.unlink()
            _write(project, layout, facets)
            history = recorded(commit, records) if recorded else {"current": {"gitCommit": commit, "facets": records}}
            entry = {
                "address": PROXY,
                "facetSrc": [f"src/{name}.sol:{name}" for name in facets],
                "deployments": {"314": history},
            }
            ledger = tmp_path / "josuke.json"
            ledger.write_text(json.dumps([entry]))
            return CliRunner().invoke(main, ["check", "-f", str(ledger)])

        return check

    return deploy


READ_COUNT = "function count() external view returns (uint256) { return uint256(count_); }"


def test_unchanged_storage_passes(deployed):
    check = deployed("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    result = check("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    assert result.exit_code == 0, result.output
    assert "layout       vs current" in result.output
    assert "2 variables checked" in result.output


def test_retyping_a_variable_no_recorded_facet_reads_fails(deployed):
    # A never reads `spare_`, so its bytecode stays the same when the type changes.
    check = deployed("address internal owner_;\nuint256 internal spare_;", {"A": ""})
    result = check(
        "address internal owner_;\nint256 internal spare_;",
        {"A": "", "B": "function spare() external view returns (int256) { return spare_; }"},
    )
    assert result.exit_code == 1, result.output
    assert "declares int256 spare_ over uint256 spare_ (src/A.sol:A at current" in result.output


def test_appending_storage_passes(deployed):
    check = deployed("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    result = check(
        "address internal owner_;\nuint256 internal count_;\nuint64 internal extra_;",
        {"A": READ_COUNT + "\nfunction extra() external view returns (uint64) { return extra_; }"},
    )
    assert result.exit_code == 0, result.output
    assert "2 variables checked" in result.output
    assert "WARNING" not in result.output


def test_changing_a_type_fails(deployed):
    check = deployed("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    result = check("address internal owner_;\nint256 internal count_;", {"A": READ_COUNT})
    assert result.exit_code == 1, result.output
    assert "storage slot 1: src/A.sol:A declares int256 count_ over uint256 count_ (src/A.sol:A at current" in result.output


def test_inserting_a_variable_fails(deployed):
    check = deployed("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    result = check("address internal owner_;\nuint256 internal inserted_;\nuint256 internal count_;", {"A": READ_COUNT})
    assert result.exit_code == 1, result.output
    assert "storage slot 1: uint256 count_ (src/A.sol:A at current" in result.output
    assert "moves to slot 2" in result.output
    assert "is renamed" not in result.output


def test_renaming_warns(deployed):
    check = deployed("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    result = check(
        "address internal admin_;\nuint256 internal count_;",
        {"A": READ_COUNT + "\nfunction admin() external view returns (address) { return admin_; }"},
    )
    assert result.exit_code == 0, result.output
    assert "1 renamed" in result.output
    assert "address owner_ (src/A.sol:A at current" in result.output
    assert "is renamed in address admin_" in result.output


def test_dropping_a_facet_warns_about_its_storage(deployed):
    tally = "uint64 internal tally_;\nfunction tally() external view returns (uint64) { return tally_; }"
    check = deployed("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT, "T": tally})
    result = check("address internal owner_;\nuint256 internal count_;", {"A": READ_COUNT})
    assert result.exit_code == 0, result.output
    assert "1 dropped" in result.output
    assert "nothing declares uint64 tally_ (src/T.sol:T at current" in result.output


STRUCT = "struct S {{ uint256 a; {more} }}\nmapping(uint256 => S) internal byId_;\nS internal single_;\n{after}"
READ_STRUCT = "function a(uint256 id) external view returns (uint256) { return byId_[id].a + single_.a; }"


def test_growing_a_struct_passes_where_its_new_bytes_are_free(deployed):
    check = deployed(STRUCT.format(more="", after=""), {"A": READ_STRUCT})
    result = check(STRUCT.format(more="uint256 b;", after=""), {"A": READ_STRUCT})
    assert result.exit_code == 0, result.output
    assert "2 grown" in result.output  # the mapping's values and `single_`


def test_growing_a_struct_over_the_next_variable_fails(deployed):
    check = deployed(STRUCT.format(more="", after="uint256 internal next_;"), {"A": READ_STRUCT})
    result = check(STRUCT.format(more="uint256 b;", after="uint256 internal next_;"), {"A": READ_STRUCT})
    assert result.exit_code == 1, result.output
    assert "uint256 next_ (src/A.sol:A at current" in result.output
    assert "moves to slot 3" in result.output
    assert "declares struct Layout.S single_ over uint256 next_" in result.output


def test_a_struct_may_move_to_another_contract(deployed):
    check = deployed("struct S { uint256 a; address b; } S internal data;", {"A": ""})
    result = check("", {"A": "struct S { uint256 a; address b; } S internal data;"})
    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output


def test_swapping_struct_members_of_one_type_fails():
    u = ("value", 32, "uint256")
    old = (("struct", 64, (("a", 0, 0, 1), ("b", 1, 0, 1))), u)
    assert not _fit(old, (("struct", 64, (("b", 0, 0, 1), ("a", 1, 0, 1))), u), set())
    assert _fit(old, (("struct", 64, (("x", 0, 0, 1), ("b", 1, 0, 1))), u), notes := set())
    assert notes == {"renamed"}


def test_growing_array_elements_shifts_them():
    small = ("struct", 32, (("a", 0, 0, 2),))
    big = ("struct", 64, (("a", 0, 0, 2), ("b", 1, 0, 2)))
    u = ("value", 32, "uint256")
    assert not _fit((("dynamic_array", 32, 1), small, u), (("dynamic_array", 32, 1), big, u), set())
    assert _fit((("mapping", 32, 2, 1), small, u), (("mapping", 32, 2, 1), big, u), notes := set())
    assert notes == {"grown"}


UDVT = "type {name} is {underlying};\n{name} internal amount_;"
READ_AMOUNT = "function amount() external view returns (bytes32) { return bytes32(uint256(uint160(address(this)))) ^ keccak256(abi.encode(amount_)); }"


def test_retyping_a_user_defined_value_type_fails(deployed):
    check = deployed(UDVT.format(name="Amount", underlying="uint256"), {"A": READ_AMOUNT})
    result = check(UDVT.format(name="Amount", underlying="int256"), {"A": READ_AMOUNT})
    assert result.exit_code == 1, result.output
    assert "declares Layout.Amount amount_ over Layout.Amount amount_" in result.output


def test_renaming_a_user_defined_value_type_passes(deployed):
    check = deployed(UDVT.format(name="Amount", underlying="uint256"), {"A": READ_AMOUNT})
    result = check(UDVT.format(name="Balance", underlying="uint256"), {"A": READ_AMOUNT})
    assert result.exit_code == 0, result.output


def test_legacy_storage_is_checked_beside_current_at_one_commit(deployed):
    check = deployed("address internal owner_;", {"Mono": "uint256 internal legacy_;", "A": "uint256 internal a_;"})

    def recorded(commit, records):
        return {
            "legacy": {"source": "src/Mono.sol:Mono", "gitCommit": commit},
            "current": {"gitCommit": commit, "facets": {"src/A.sol:A": records["src/A.sol:A"]}},
        }

    result = check("address internal owner_;", {"A": "int256 internal a_;"}, recorded)
    assert result.exit_code == 1, result.output
    assert "over uint256 legacy_ (src/Mono.sol:Mono at legacy" in result.output
    assert "over uint256 a_ (src/A.sol:A at current" in result.output


def test_retired_facet_storage_is_still_checked(deployed):
    check = deployed("address internal owner_;", {"A": "", "T": "uint256 internal tally_;"})

    def recorded(commit, records):
        retired = {"0x" + "ee" * 20: {k: v for k, v in records["src/T.sol:T"].items() if k != "address"} | {"source": "src/T.sol:T", "gitCommit": commit}}
        return {"current": {"gitCommit": commit, "facets": {"src/A.sol:A": records["src/A.sol:A"]}}, "history": retired}

    result = check("address internal owner_;", {"A": "", "F": "address internal flag_;"}, recorded)
    assert result.exit_code == 1, result.output
    assert "declares address flag_ over uint256 tally_ (src/T.sol:T at history" in result.output


NAMESPACE = "/// @custom:storage-location erc7201:test.ns\nstruct NS {{ {members} }}"


def test_namespaced_storage_is_checked_against_the_deployment(deployed):
    check = deployed(NAMESPACE.format(members="uint256 a; uint256 b;"), {"A": ""})

    result = check(NAMESPACE.format(members="uint256 a; uint256 inserted; uint256 b;"), {"A": ""})
    assert result.exit_code == 1, result.output
    assert "declares struct Layout.NS erc7201:test.ns over struct Layout.NS erc7201:test.ns" in result.output

    result = check(NAMESPACE.format(members="uint256 a; uint256 b; uint256 c;"), {"A": ""})
    assert result.exit_code == 0, result.output
    assert "1 ERC-7201 namespace" in result.output
    assert "1 grown" in result.output


def test_one_namespace_defined_twice_must_agree(deployed):
    check = deployed("address internal owner_;", {"A": ""})
    a = NAMESPACE.format(members="uint256 a;").replace("{{", "{").replace("}}", "}")

    result = check("address internal owner_;", {"A": a, "B": a.replace("uint256 a", "address a")})
    assert result.exit_code == 1, result.output
    assert "erc7201:test.ns" in result.output

    # A historical rename is allowed, but installed facets must agree.
    result = check("address internal owner_;", {"A": a, "B": a.replace("uint256 a", "uint256 b")})
    assert result.exit_code == 1, result.output
    assert "erc7201:test.ns" in result.output


def test_legacy_view_contract_rename_preserves_layout(deployed):
    check = deployed("address public viewContractAddress;", {"Mono": ""})

    def recorded(commit, records):
        return {"legacy": {"source": "src/Mono.sol:Mono", "gitCommit": commit}}

    result = check("address internal _viewContractAddress;", {"A": "", "B": ""}, recorded)
    assert result.exit_code == 0, result.output
    assert "is renamed in address _viewContractAddress" in result.output
    assert "WARNING" in result.output


@pytest.mark.parametrize("members", [
    "uint256 value; mapping(uint256 => Node) children;",
    "uint256 value; Node[] children;",
])
def test_recursive_storage_is_compared_without_losing_nested_changes(deployed, members):
    layout = "struct Node { " + members + " } Node internal root;"
    check = deployed(layout, {"A": ""})
    result = check(layout, {"A": ""})
    assert result.exit_code == 0, result.output
    result = check(layout.replace("uint256 value", "int256 value"), {"A": ""})
    assert result.exit_code == 1, result.output
    assert "over struct Layout.Node root" in result.output


def test_recursive_mapping_value_can_grow(deployed):
    layout = "struct Node { uint256 value; mapping(uint256 => Node) children; MORE } mapping(uint256 => Node) internal roots;"
    check = deployed(layout.replace("MORE", ""), {"A": ""})
    result = check(layout.replace("MORE", "uint256 extra;"), {"A": ""})
    assert result.exit_code == 0, result.output
    assert "1 grown" in result.output


def test_fixed_array_length_changes_are_not_hidden_by_slot_rounding(deployed):
    check = deployed("uint8[2] internal values;", {"A": ""})
    result = check("uint8[1] internal values;", {"A": ""})
    assert result.exit_code == 1, result.output
    result = check("uint8[3] internal values;", {"A": ""})
    assert result.exit_code == 0, result.output
    assert "1 grown" in result.output
    result = check("", {"A": "uint8[1] internal values;", "B": "uint8[3] internal values;"})
    assert result.exit_code == 1, result.output
    assert "over src/A.sol:A's uint8[1] values" in result.output


@pytest.mark.parametrize("members, passes", [("A, B, C", True), ("A, B, C, D", True), ("A, B", False), ("A, C, B", False)])
def test_enum_ordinals_keep_their_meaning(deployed, members, passes):
    check = deployed("enum E { A, B, C } E internal status_;", {"A": ""})
    result = check("enum E { " + members + " } E internal status_;", {"A": ""})
    assert result.exit_code == (0 if passes else 1), result.output


def test_shared_type_graph_does_not_expand_exponentially(deployed):
    layout = "struct S0 { uint256 value; }\n" + "\n".join(
        f"struct S{i} {{ mapping(uint256 => S{i-1}) left; mapping(uint256 => S{i-1}) right; }}" for i in range(1, 17)
    ) + "\nS16 internal tree;"
    check = deployed(layout, {"A": ""})
    result = check(layout.replace("uint256 value", "int256 value"), {"A": ""})
    assert result.exit_code == 1, result.output
    assert "over struct Layout.S16 tree" in result.output
    from pathlib import Path
    from josuke.check import _declarations
    from josuke.layout import storage_layouts

    layouts, _ = storage_layouts(Path.cwd(), [facet_from_source_id("src/A.sol:A")])
    shape = _declarations(layouts)[0].shape
    assert len(repr(shape)) < 8000  # The source's shared graph must stay compact, including hashing.


def test_equivalent_graphs_can_share_types_differently(deployed):
    a = "struct S { uint256 value; } struct Pair { S a; S b; } Pair internal pair;"
    b = "struct S { uint256 value; } struct T { uint256 value; } struct Pair { S a; T b; } Pair internal pair;"
    check = deployed(a, {"A": ""})
    result = check("", {"A": a, "B": b})
    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output
