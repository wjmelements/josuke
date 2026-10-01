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
from josuke.broadcast import create_address
from josuke.evm import EvmRelay, _governing_makefile, evm_artifact, replay_create

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


# -- EvmRelay / replay_create ----------------------------------------------

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


DEPLOY = {"from": DEPLOYER, "nonce": "0x5", "blockOverrides": {"number": "0x64"}}


@needs_evm
def test_replay_create_reads_nothing_without_a_constructor(eth_rpc):
    replay = replay_create(_initcode("NoArgs"), DEPLOY, trace=True)
    assert (replay.reads_sender, replay.reads_address, replay.block_overrides) == (False, False, {})


@needs_evm
def test_replay_create_detects_sender(eth_rpc):
    replay = replay_create(_initcode("FromDeployer"), DEPLOY, trace=True)
    assert (replay.reads_sender, replay.reads_address) == (True, False)
    assert DEPLOYER.removeprefix("0x") in replay.runtime


@needs_evm
def test_replay_create_detects_own_address_and_creates_at_nonce(eth_rpc):
    replay = replay_create(_initcode("SelfAddress"), DEPLOY, trace=True)
    assert replay.reads_address
    assert create_address(DEPLOYER, 5)[2:].lower() in replay.runtime


@needs_evm
def test_replay_create_reports_block_values_read_but_not_chain_id(eth_rpc):
    replay = replay_create(_initcode("DeployedAt"), DEPLOY, trace=True)
    assert replay.block_overrides == {"time": hex(eth_rpc.timestamp_base + 0x64)}  # block 0x64's header
    assert f"{314:064x}" in replay.runtime  # CHAINID asked of the node, not recorded


@needs_evm
def test_replay_create_without_trace_reports_no_reads(eth_rpc):
    replay = replay_create(_initcode("SelfAddress"), DEPLOY)
    assert (replay.reads_sender, replay.reads_address) == (False, False)


@needs_evm
def test_evm_relay_returns_json_results_without_forwarding_them(eth_rpc):
    with EvmRelay(json_output=True) as relay:
        result = json.loads(relay.call({"from": DEPLOYER, "data": _initcode("NoArgs")}))
    assert set(result) >= {"status", "returnData"}
    assert all("returnData" not in str(call) for call in eth_rpc.calls)


@needs_evm
def test_evm_relay_streams_a_trace_longer_than_a_pipe_buffer(eth_rpc):
    steps = []
    with EvmRelay(on_trace=steps.append) as relay:
        relay.call({"data": "5f50" * 20000 + "5f5ff3"})  # 40000 PUSH0/POP steps
    assert len(steps) > 40000 and "output" in steps[-1]  # every step, then the summary


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
