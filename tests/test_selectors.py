import shutil
from json import load
from pathlib import Path

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak

from josuke.evm import execute
from josuke.selectors import Selector
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
    a = Selector("0x12345678", "f(struct A.S)")
    b = Selector("0x12345678", "f(struct B.S)")
    assert a == b and len({a, b}) == 1
