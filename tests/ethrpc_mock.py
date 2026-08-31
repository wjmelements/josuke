"""Shared JSON-RPC mock for exercising josuke's ``ETH_RPC_URL`` calls without a node.

Patch it over ``requests.post`` in whichever module is under test, e.g.::

    rpc = MockEthRpc()
    rpc.set_code(ADDRESS, "0x60006000...")
    with patch("josuke.delegate.post", rpc):
        ...
"""

import json


def norm_addr(a: str) -> str:
    return a.lower()


def norm_slot(s: str) -> str:
    return hex(int(s, 16))


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class MockEthRpc:
    def __init__(self, block_number: int = 0x1312D00):
        self.calls = []
        self.block_number = block_number
        self.code = {}
        self.storage = {}
        self.nonce = {}
        self.balance = {}

    # -- request plumbing ----------------------------------------------------

    def __call__(self, url, *args, **kwargs):
        payload = self._extract_payload(args, kwargs)
        self.calls.append(payload)

        if isinstance(payload, list):
            body = [self._dispatch(req) for req in payload]
        else:
            body = self._dispatch(payload)

        return FakeResponse(200, json.dumps(body))

    @staticmethod
    def _extract_payload(args, kwargs):
        """Tolerate whichever way the caller ends up invoking post()."""
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
        return self.code.get(norm_addr(params[0]), "0x")

    def _rpc_eth_getTransactionCount(self, params):
        return self.nonce.get(norm_addr(params[0]), "0x1")

    def _rpc_eth_getBalance(self, params):
        return self.balance.get(norm_addr(params[0]), "0x0")

    def _rpc_eth_getStorageAt(self, params):
        key = (norm_addr(params[0]), norm_slot(params[1]))
        return self.storage.get(key, "0x" + "00" * 32)

    # -- test helpers ------------------------------------------------------

    def set_code(self, address: str, code: str):
        self.code[norm_addr(address)] = code if code.startswith("0x") else "0x" + code

    def set_storage(self, address: str, slot: str, value: str):
        self.storage[(norm_addr(address), norm_slot(slot))] = value

    def requests_for(self, method: str):
        out = []
        for payload in self.calls:
            for req in payload if isinstance(payload, list) else [payload]:
                if req.get("method") == method:
                    out.append(req)
        return out
