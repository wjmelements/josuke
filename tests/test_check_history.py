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

        def check(layout, facets):
            for path in (project / "src").glob("*.sol"):
                path.unlink()
            _write(project, layout, facets)
            entry = {
                "address": PROXY,
                "facetSrc": [f"src/{name}.sol:{name}" for name in facets],
                "deployments": {"314": {"current": {"gitCommit": commit, "facets": records}}},
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


def test_a_struct_may_move_to_another_contract():
    old = ("struct", 64, (("a", 0, 0, ("value", 32, "uint256")), ("b", 1, 0, ("value", 32, "address"))))
    assert _fit(old, old, notes := set()) and not notes


def test_growing_array_elements_shifts_them():
    small = ("struct", 32, (("a", 0, 0, ("value", 32, "uint256")),))
    big = ("struct", 64, (("a", 0, 0, ("value", 32, "uint256")), ("b", 1, 0, ("value", 32, "uint256"))))
    assert not _fit(("dynamic_array", 32, small), ("dynamic_array", 32, big), set())
    assert _fit(("mapping", 32, ("value", 32, "uint256"), small), ("mapping", 32, ("value", 32, "uint256"), big), notes := set())
    assert notes == {"grown"}
