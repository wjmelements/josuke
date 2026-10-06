import json
import pathlib
import shutil

import pytest
from click.testing import CliRunner

from josuke.abi import SELECTORS_ABI, merge_abis
from josuke.cli import main

ROOT = pathlib.Path(__file__).parent / "fixtures" / "check-project"
PROXY = "0x2222222222222222222222222222222222222222"

requires_forge = pytest.mark.skipif(shutil.which("forge") is None, reason="requires the `forge` binary")


def fn(name, inputs=(), outputs=(), mutability="nonpayable"):
    return {
        "type": "function",
        "name": name,
        "inputs": [{"name": "", "type": t, "internalType": t} for t in inputs],
        "outputs": [{"name": "", "type": t, "internalType": t} for t in outputs],
        "stateMutability": mutability,
    }


def event(name):
    return {"type": "event", "name": name, "inputs": [], "anonymous": False}


def test_merges_facets_and_adds_selectors():
    merged = merge_abis({"A.sol:A": [fn("a")], "B.sol:B": [fn("b"), event("E")]})
    assert merged == [fn("a"), fn("b"), event("E"), SELECTORS_ABI]


def test_keeps_a_facets_own_selectors():
    own = fn("selectors", outputs=["bytes4[]"], mutability="pure")
    assert merge_abis({"S.evm": [own]}) == [own]


def test_drops_what_the_proxy_never_dispatches_to():
    abi = [
        {"type": "constructor", "inputs": [], "stateMutability": "nonpayable"},
        {"type": "fallback", "stateMutability": "payable"},
        {"type": "receive", "stateMutability": "payable"},
        fn("a"),
    ]
    assert merge_abis({"A.sol:A": abi}) == [fn("a"), SELECTORS_ABI]


def test_events_and_errors_appear_once():
    error = {"type": "error", "name": "Nope", "inputs": []}
    merged = merge_abis({"A.sol:A": [event("E"), error], "B.sol:B": [event("E"), error]})
    assert merged == [event("E"), error, SELECTORS_ABI]


def test_shared_selector_warns_and_keeps_the_first(capsys):
    merged = merge_abis({"A.sol:A": [fn("a", outputs=["uint256"])], "B.sol:B": [fn("a", outputs=["bool"])]})
    assert merged == [fn("a", outputs=["uint256"]), SELECTORS_ABI]
    assert "a() is exported by both A.sol:A and B.sol:B; keeping A.sol:A" in capsys.readouterr().err


def test_identical_shared_selector_warns_without_keeping(capsys):
    merged = merge_abis({"A.sol:A": [fn("a")], "B.sol:B": [fn("a")]})
    assert merged == [fn("a"), SELECTORS_ABI]
    err = capsys.readouterr().err
    assert "exported by both A.sol:A and B.sol:B" in err
    assert "keeping" not in err


def _abi(tmp_path, monkeypatch, ledger, *args):
    monkeypatch.chdir(ROOT)
    path = tmp_path / "josuke.json"
    path.write_text(json.dumps(ledger))
    return CliRunner().invoke(main, ["abi", *args, "-f", str(path)])


@requires_forge
def test_prints_the_merged_abi(tmp_path, monkeypatch):
    result = _abi(tmp_path, monkeypatch, [{"address": PROXY, "facetSrc": ["src/facets/*.sol"]}], PROXY)
    assert result.exit_code == 0, result.output
    names = [item["name"] for item in json.loads(result.stdout)]
    assert sorted(names) == ["count", "increment", "owner", "selectors", "transferOwnership"]


def test_unknown_proxy_fails(tmp_path, monkeypatch):
    result = _abi(tmp_path, monkeypatch, [], PROXY)
    assert result.exit_code != 0
    assert f"{PROXY} is not in" in result.output


def test_requires_an_address(tmp_path, monkeypatch):
    result = _abi(tmp_path, monkeypatch, [])
    assert result.exit_code != 0
    assert "Missing argument 'ADDRESS'" in result.output
