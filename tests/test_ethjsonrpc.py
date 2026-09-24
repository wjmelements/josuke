"""Tests for josuke.ethjsonrpc.rpc_batch, with the HTTP layer faked."""

import pytest

from josuke import ethjsonrpc


class Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    sent = {}

    def fake_post(url, json):
        sent["payload"] = json
        return sent["response"]

    monkeypatch.setattr(ethjsonrpc.requests, "post", fake_post)
    return sent


def test_rpc_batch_returns_results_in_call_order(post):
    # answers may come back in any order; ids tie them to calls
    post["response"] = Response([{"id": 1, "result": "0x2"}, {"id": 0, "result": "0x1"}])

    assert ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])]) == ["0x1", "0x2"]
    assert post["payload"] == [
        {"id": 0, "jsonrpc": "2.0", "method": "eth_chainId", "params": []},
        {"id": 1, "jsonrpc": "2.0", "method": "eth_blockNumber", "params": []},
    ]


def test_rpc_batch_fails_on_any_error(post):
    post["response"] = Response(
        [{"id": 0, "result": "0x1"}, {"id": 1, "error": {"code": 3, "message": "execution reverted"}}]
    )
    with pytest.raises(ethjsonrpc.RpcError, match="eth_estimateGas .*execution reverted"):
        ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_estimateGas", [{"data": "0x00"}])])


def test_rpc_batch_fails_on_missing_answer(post):
    post["response"] = Response([{"id": 0, "result": "0x1"}])
    with pytest.raises(ethjsonrpc.RpcError, match="eth_blockNumber: no response"):
        ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])])


def test_rpc_batch_fails_when_node_rejects_the_batch(post):
    post["response"] = Response({"id": None, "error": {"code": -32600, "message": "batch not supported"}})
    with pytest.raises(ethjsonrpc.RpcError, match="batch not supported"):
        ethjsonrpc.rpc_batch([("eth_chainId", [])])


def test_rpc_batch_fails_on_http_error(post):
    post["response"] = Response(None, status_code=502)
    with pytest.raises(ethjsonrpc.RpcError, match="HTTP 502"):
        ethjsonrpc.rpc_batch([("eth_chainId", [])])
