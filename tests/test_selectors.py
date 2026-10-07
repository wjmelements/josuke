import shutil
from json import load
from pathlib import Path

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak

from josuke.evm import execute
from josuke.selectors import Selector, SelectorCollision
from josuke.erc8167 import selectors_method

ABI_PATH = Path(__file__).parent / "fixtures" / "FilecoinPayV1.abi.json"

requires_evm = pytest.mark.skipif(
    shutil.which("evm") is None, reason="requires the `evm` binary"
)


@pytest.fixture
def selectors():
    with open(ABI_PATH) as f:
        abi = load(f)
    functions = [item for item in abi if item["type"] == "function"]
    return list(map(Selector.from_abi, functions))


@requires_evm
def test_selectors_method(selectors):
    method = selectors_method(selectors)
    result = execute(method.hex())
    assert result == abi_encode(
        ["bytes4[]"],
        [[bytes.fromhex(selector.selector.removeprefix("0x")) for selector in selectors]],
    ).hex()


def _fn(name, *inputs):
    return {"type": "function", "name": name, "inputs": list(inputs)}


def test_from_abi_expands_struct_parameters():
    struct = {
        "type": "tuple",
        "internalType": "struct Lib.Rail",
        "components": [
            {"type": "uint256", "internalType": "uint256"},
            {"type": "tuple[]", "internalType": "struct Lib.Leg[]", "components": [
                {"type": "address", "internalType": "address"},
                {"type": "bytes4[2]", "internalType": "bytes4[2]"},
            ]},
        ],
    }
    selector = Selector.from_abi(_fn("f", struct))
    assert selector.selector == "0x" + keccak(text="f((uint256,(address,bytes4[2])[]))")[:4].hex()
    assert selector.expressive == "f(struct Lib.Rail)"


def test_from_abi_struct_array():
    arg = {"type": "tuple[3]", "components": [{"type": "uint8"}]}
    assert Selector.from_abi(_fn("g", arg)).selector == "0x" + keccak(text="g((uint8)[3])")[:4].hex()


def test_from_abi_without_internal_type_falls_back_to_type():
    assert Selector.from_abi(_fn("h", {"type": "uint256"})).expressive == "h(uint256)"


def test_same_selector_with_different_internal_types_is_equal():
    a = Selector("0x12345678", "f(struct A.S)", "f((uint256))")
    b = Selector("0x12345678", "f(struct B.S)", "f((uint256))")
    assert a == b and len({a, b}) == 1


def test_colliding_signatures_raise():
    # A known 4-byte collision: both hash to 0x42966c68.
    Selector.from_abi(_fn("burn", {"type": "uint256"}))
    with pytest.raises(SelectorCollision, match="burn\\(uint256\\) and collate_propagate_storage\\(bytes16\\)"):
        Selector.from_abi(_fn("collate_propagate_storage", {"type": "bytes16"}))
