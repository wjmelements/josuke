import json
from unittest.mock import patch

import pytest

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


def _norm_addr(a: str) -> str:
    return a.lower()


def _norm_slot(s: str) -> str:
    return hex(int(s, 16))


class MockEthRpc:
    def __init__(self):
        self.calls = []
        self.block_number = BLOCK_NUMBER
        self.code = {_norm_addr(PROXY_ADDRESS): "0x" + PROXY_CODE}
        self.storage = {}
        self.nonce = {}
        self.balance = {}

    def __call__(self, url, *args, **kwargs):
        payload = self._extract_payload(args, kwargs)
        self.calls.append(payload)

        if isinstance(payload, list):
            body = [self._dispatch(req) for req in payload]
        else:
            body = self._dispatch(payload)

        return _FakeResponse(200, json.dumps(body))

    @staticmethod
    def _extract_payload(args, kwargs):
        """Tolerate whichever way storage.py ends up calling post()."""
        if "json" in kwargs:
            return kwargs["json"]
        if "data" in kwargs:
            return json.loads(kwargs["data"])
        if args:
            body = args[0]
            return body if isinstance(body, (dict, list)) else json.loads(body)
        raise AssertionError("no request body passed to post()")

    def _dispatch(self, req):
        method = req["method"]
        params = req.get("params", [])
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise AssertionError(f"unexpected JSON-RPC method: {method}")
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": handler(params)}

    def _rpc_eth_blockNumber(self, params):
        return hex(self.block_number)

    def _rpc_eth_getCode(self, params):
        return self.code.get(_norm_addr(params[0]), "0x")

    def _rpc_eth_getTransactionCount(self, params):
        return self.nonce.get(_norm_addr(params[0]), "0x1")

    def _rpc_eth_getBalance(self, params):
        return self.balance.get(_norm_addr(params[0]), "0x0")

    def _rpc_eth_getStorageAt(self, params):
        key = (_norm_addr(params[0]), _norm_slot(params[1]))
        return self.storage.get(key, "0x" + "00" * 32)

    # -- test helpers ---------------------------------------------------

    def set_storage(self, address: str, slot: str, value: str):
        self.storage[(_norm_addr(address), _norm_slot(slot))] = value

    def requests_for(self, method: str):
        out = []
        for payload in self.calls:
            for req in (payload if isinstance(payload, list) else [payload]):
                if req.get("method") == method:
                    out.append(req)
        return out


class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def eth_rpc(monkeypatch):
    # Patches `josuke.storage.post` and provides ETH_RPC_URL.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc()
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
