"""Tests for josuke.evm's .evm-facet artifact build and the `evm -nx` relay."""

import json
import pathlib
import shutil
import subprocess
import textwrap
from unittest.mock import patch

import click
import pytest

from ethrpc_mock import MockEthRpc
from josuke import evm
from josuke.evm import EvmRelay, _governing_makefile, deployer_derived, evm_artifact

FIXTURE_ROOT = pathlib.Path(__file__).parent / "fixtures" / "forge-project"


def _initcode(contract: str) -> str:
    out = subprocess.run(
        ["forge", "inspect", f"src/{contract}.sol:{contract}", "bytecode"],
        cwd=FIXTURE_ROOT, text=True, capture_output=True, check=True,
    ).stdout.strip()
    return out.removeprefix("0x")

ARTIFACT = {
    "bytecode": {"object": "0xdeadbeef"},
    "abi": [{"type": "function", "name": "impl", "inputs": [], "outputs": []}],
}


def test_governing_makefile_picks_nearest_ancestor(tmp_path):
    (tmp_path / "Makefile").write_text("")
    sub = tmp_path / "lib" / "dep"
    (sub / "src").mkdir(parents=True)
    (sub / "Makefile").write_text("")
    assert _governing_makefile(sub / "src" / "X.evm", tmp_path) == sub


def test_governing_makefile_none_above_root(tmp_path):
    (tmp_path / "Makefile").write_text("")  # this is the parent of root
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    assert _governing_makefile(root / "src" / "X.evm", root) is None


def _stub_makefile(directory, stem):
    """A Makefile whose artifact rule just writes the canned ARTIFACT json."""
    (directory / "Makefile").write_text(
        textwrap.dedent(f"""\
        out/{stem}.evm/{stem}.json:
        \tmkdir -p out/{stem}.evm
        \tprintf '%s' '{json.dumps(ARTIFACT)}' > $@
        """)
    )


needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="requires make")


@needs_make
def test_evm_artifact_builds_and_reads(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    _stub_makefile(tmp_path, "Impl")

    got = evm_artifact(src / "Impl.evm", tmp_path)
    assert got == {"initcode": "deadbeef", "abi": ARTIFACT["abi"]}


@needs_make
def test_evm_artifact_memoises_the_build(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    _stub_makefile(tmp_path, "Impl")

    evm_artifact(src / "Impl.evm", tmp_path)
    artifact = tmp_path / "out" / "Impl.evm" / "Impl.json"
    artifact.write_text(json.dumps({**ARTIFACT, "bytecode": {"object": "0xcafe"}}))

    # second call must not re-run make (which would overwrite our edit back)
    assert evm_artifact(src / "Impl.evm", tmp_path)["initcode"] == "cafe"


def test_evm_artifact_without_makefile_raises(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    with pytest.raises(click.ClickException, match="no Makefile"):
        evm_artifact(src / "Impl.evm", tmp_path)


@needs_make
def test_evm_artifact_without_abi_raises(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    (tmp_path / "Makefile").write_text(
        textwrap.dedent("""\
        out/Impl.evm/Impl.json:
        \tmkdir -p out/Impl.evm
        \tprintf '%s' '{"bytecode":{"object":"0x00"},"deployedBytecode":{"object":"0x00"}}' > $@
        """)
    )
    with pytest.raises(click.ClickException, match="no ABI"):
        evm_artifact(src / "Impl.evm", tmp_path)


# -- EvmRelay / deployer_derived -------------------------------------------

needs_evm = pytest.mark.skipif(
    shutil.which("evm") is None or shutil.which("forge") is None,
    reason="requires the `evm` and `forge` binaries",
)

DEPLOYER = "0x" + "aa" * 20


@pytest.fixture
def eth_rpc(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc()
    with patch("josuke.evm.post", rpc):
        yield rpc


@needs_evm
def test_deployer_derived_true_when_constructor_reads_sender(eth_rpc):
    assert deployer_derived(_initcode("FromDeployer"), DEPLOYER) is True


@needs_evm
def test_deployer_derived_false_without_a_constructor(eth_rpc):
    assert deployer_derived(_initcode("NoArgs"), DEPLOYER) is False


@needs_evm
def test_evm_relay_caches_rpc_results_across_processes(eth_rpc):
    initcode = _initcode("NoArgs")
    cache = {}
    with EvmRelay(cache=cache) as relay:
        relay.call({"from": DEPLOYER, "data": initcode})
    first = len(eth_rpc.calls)
    with EvmRelay(cache=cache) as relay:  # identical run, shared cache
        relay.call({"from": DEPLOYER, "data": initcode})
    assert len(eth_rpc.calls) == first  # every request served from the cache


@needs_evm
def test_evm_relay_reports_exchanges(eth_rpc):
    seen = []
    with EvmRelay() as relay:
        relay.call(
            {"from": DEPLOYER, "data": _initcode("NoArgs")},
            on_exchange=lambda req, resp: seen.append(req),
        )
    methods = {r["method"] for batch in seen for r in (batch if isinstance(batch, list) else [batch])}
    assert "eth_blockNumber" in methods and "eth_getCode" in methods
