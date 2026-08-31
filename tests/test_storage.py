from unittest.mock import patch

import pytest

from ethrpc_mock import MockEthRpc
from josuke.selectors import Selector
from josuke.storage import ProxyStorage


PROXY_ADDRESS = "0x1A4E1a4e1A4E1a4e1a4E1a4e1A4e1A4E1a4E1A4e"

PROXY_CODE = (
    "5f5f365f60045f5f3760405f2054806023575f51602052635416eb985f526024601c"
    "fd5b365f5f375af43d5f5f3e6034573d5ffd5b3d5ff3"
)

BLOCK_NUMBER = 0x1312D00

ABI = [
  {
    "type": "function",
    "name": "cancelPendingWeight",
    "inputs": [
      {
        "name": "op",
        "type": "uint8",
        "internalType": "enum PendingOp"
      }
    ],
    "outputs": [],
    "stateMutability": "nonpayable"
  },
  {
    "type": "function",
    "name": "quarterlyGateCheck",
    "inputs": [],
    "outputs": [],
    "stateMutability": "nonpayable"
  },
]


@pytest.fixture
def eth_rpc(monkeypatch):
    # Patches `josuke.storage.post` and provides ETH_RPC_URL.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc(block_number=BLOCK_NUMBER)
    rpc.set_code(PROXY_ADDRESS, PROXY_CODE)
    with patch("josuke.storage.post", rpc):
        yield rpc


@pytest.mark.timeout(2)
def test_fetch(eth_rpc):
    selectors = map(Selector.from_abi, ABI)
    storage = ProxyStorage(PROXY_ADDRESS)

    storage.fetch(selectors)

    assert eth_rpc.requests_for("eth_getCode")[0]["params"][0] == PROXY_ADDRESS.lower()
    for selector in selectors:
        assert selector.selector in storage.storage_keys[selector.selector]
        assert storage.storage_values[selector.selector] == "0x" + "00" * 32
