"""Verification tests for josuke.delegate.

`Delegate.fetch()` is exercised against the shared JSON-RPC mock.
`Delegate.matches_source()` is almost entirely a pipeline of real `forge inspect`
and real `evm -x` invocations, so it is exercised end-to-end against a committed
Foundry fixture project (tests/fixtures/forge-project). The tests are skipped when
either binary is missing.
"""

import pathlib
import shutil
import subprocess
from unittest.mock import patch

import pytest
from eth_abi import encode as abi_encode

from ethrpc_mock import MockEthRpc
from josuke.delegate import (
    ContractSource,
    Delegate,
    UnconfiguredParameter,
    source_map,
)
from josuke.evm import execute

FIXTURE_ROOT = pathlib.Path(__file__).parent / "fixtures" / "forge-project"

pytestmark = pytest.mark.skipif(
    shutil.which("forge") is None or shutil.which("evm") is None,
    reason="requires the `forge` and `evm` binaries",
)

# An arbitrary but valid-looking deployment address.
ADDRESS = "0x1A4E1a4e1A4E1a4e1a4E1a4e1A4e1A4E1a4E1A4e"


def _forge_inspect(contract: str, field: str, *extra: str) -> str:
    out = subprocess.run(
        ["forge", "inspect", contract, field, *extra],
        cwd=FIXTURE_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    return out.strip()


def _deployed_code(contract: str, arg_types=(), arg_values=()) -> str:
    """The runtime bytecode a real deployment of `contract` would leave on chain."""
    initcode = _forge_inspect(contract, "bytecode").removeprefix("0x")
    if arg_types:
        initcode += abi_encode(list(arg_types), list(arg_values)).hex()
    return execute(initcode)


@pytest.fixture(autouse=True)
def _isolate_source_map():
    """delegate.source_map is module-global; keep tests from leaking into each other."""
    saved = dict(source_map)
    source_map.clear()
    yield
    source_map.clear()
    source_map.update(saved)


@pytest.fixture
def eth_rpc(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc()
    with patch("josuke.delegate.post", rpc):
        yield rpc


# -- Delegate / ContractSource basics -------------------------------------------


def test_delegate_registers_in_source_map():
    source = ContractSource("src/NoArgs.sol", "NoArgs", root=str(FIXTURE_ROOT))
    delegate = Delegate(ADDRESS, source)
    assert source_map[ADDRESS] is delegate
    assert delegate.address20 == ADDRESS.removeprefix("0x").lower()


def test_fetch_populates_deployed_bytecode(eth_rpc):
    source = ContractSource("src/NoArgs.sol", "NoArgs", root=str(FIXTURE_ROOT))
    delegate = Delegate(ADDRESS, source)
    eth_rpc.set_code(ADDRESS, "0x6080604052")

    delegate.fetch()

    assert delegate.deployed_bytecode == "6080604052"
    assert eth_rpc.requests_for("eth_getCode")[0]["params"][0] == ADDRESS


# -- matches_source(): no constructor ------------------------------------------


def test_matches_source_true_without_constructor(eth_rpc):
    source = ContractSource("src/NoArgs.sol", "NoArgs", root=str(FIXTURE_ROOT))
    delegate = Delegate(ADDRESS, source)
    # A contract with no immutables: the on-chain runtime code is exactly
    # forge's deployedBytecode.
    eth_rpc.set_code(ADDRESS, _forge_inspect("src/NoArgs.sol:NoArgs", "deployedBytecode"))
    delegate.fetch()

    assert delegate.matches_source() is True


def test_matches_source_false_on_mutated_onchain_code(eth_rpc):
    source = ContractSource("src/NoArgs.sol", "NoArgs", root=str(FIXTURE_ROOT))
    delegate = Delegate(ADDRESS, source)
    good = _forge_inspect("src/NoArgs.sol:NoArgs", "deployedBytecode").removeprefix("0x")
    mutated = ("ff" if good[:2] != "ff" else "00") + good[2:]
    eth_rpc.set_code(ADDRESS, mutated)
    delegate.fetch()

    assert delegate.matches_source() is False


# -- matches_source(): constructor + immutables -------------------------------

A_VALUE = 123
B_VALUE = "0x00000000000000000000000000000000000000ff"


def test_matches_source_true_with_immutables(eth_rpc):
    source = ContractSource(
        "src/WithArgs.sol",
        "WithArgs",
        config={"_a": A_VALUE, "_b": B_VALUE},
        root=str(FIXTURE_ROOT),
    )
    delegate = Delegate(ADDRESS, source)
    eth_rpc.set_code(
        ADDRESS,
        _deployed_code(
            "src/WithArgs.sol:WithArgs",
            ("uint256", "address"),
            (A_VALUE, B_VALUE),
        ),
    )
    delegate.fetch()

    assert delegate.matches_source() is True


def test_matches_source_false_on_wrong_immutable(eth_rpc):
    # On-chain code was deployed with _a = A_VALUE, but the config claims 999,
    # so the recomputed immutables differ. This is the non-circular check.
    source = ContractSource(
        "src/WithArgs.sol",
        "WithArgs",
        config={"_a": 999, "_b": B_VALUE},
        root=str(FIXTURE_ROOT),
    )
    delegate = Delegate(ADDRESS, source)
    eth_rpc.set_code(
        ADDRESS,
        _deployed_code(
            "src/WithArgs.sol:WithArgs",
            ("uint256", "address"),
            (A_VALUE, B_VALUE),
        ),
    )
    delegate.fetch()

    assert delegate.matches_source() is False


def test_matches_source_raises_on_unconfigured_parameter(eth_rpc):
    source = ContractSource(
        "src/WithArgs.sol",
        "WithArgs",
        config={"_a": A_VALUE},  # _b missing
        root=str(FIXTURE_ROOT),
    )
    delegate = Delegate(ADDRESS, source)
    eth_rpc.set_code(ADDRESS, "0x00")
    delegate.fetch()

    with pytest.raises(UnconfiguredParameter):
        delegate.matches_source()
