import shutil
from json import load
from pathlib import Path

import pytest
from eth_abi import encode as abi_encode

from josuke.evm import execute
from josuke.selectors import Selector
from josuke.erc8167 import selectors_method

ABI_PATH = Path(__file__).parent / "fixtures" / "FilecoinPayV1.abi.json"

pytestmark = pytest.mark.skipif(
    shutil.which("evm") is None, reason="requires the `evm` binary"
)


@pytest.fixture
def selectors():
    with open(ABI_PATH) as f:
        abi = load(f)
    functions = [item for item in abi if item["type"] == "function"]
    return list(map(Selector.from_abi, functions))


def test_selectors_method(selectors):
    method = selectors_method(selectors)
    result = execute(method.hex())
    assert result == abi_encode(
        ["bytes4[]"],
        [[bytes.fromhex(selector.selector.removeprefix("0x")) for selector in selectors]],
    ).hex()
