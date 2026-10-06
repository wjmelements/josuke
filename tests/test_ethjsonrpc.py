"""Tests for josuke.ethjsonrpc's batches, with the HTTP layer faked."""

import json as json_module

import pytest

from ethrpc_mock import FakeResponse, MockEthRpc
from josuke import ethjsonrpc

ACCOUNT = "0x" + "aa" * 20


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

    monkeypatch.setattr(ethjsonrpc, "_batch_limits", {})
    monkeypatch.setattr(ethjsonrpc, "post", fake_post)
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
    with pytest.raises(ethjsonrpc.RpcError, match="no answer to eth_blockNumber"):
        ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])])


def test_rpc_batch_fails_when_node_rejects_the_batch(post):
    post["response"] = Response({"id": None, "error": {"code": -32600, "message": "batch not supported"}})
    with pytest.raises(ethjsonrpc.RpcError, match="batch not supported"):
        ethjsonrpc.rpc_batch([("eth_chainId", [])])


def test_rpc_batch_fails_on_http_error(post):
    post["response"] = Response(None, status_code=502)
    with pytest.raises(ethjsonrpc.RpcError, match="HTTP 502"):
        ethjsonrpc.rpc_batch([("eth_chainId", [])])


def _storage_batch(count: int) -> list[dict]:
    return [
        {"jsonrpc": "2.0", "id": i, "method": "eth_getStorageAt", "params": [ACCOUNT, f"0x{i:064x}", "0x10"]}
        for i in range(count)
    ]


def _error(code: int, message: str, id=None) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


NGINX_413 = "<html><head><title>413 Request Entity Too Large</title></head></html>"
# How each node rejects a batch of more requests than it takes.
REJECTIONS = {
    "geth": lambda batch: (200, [_error(-32600, "batch too large", batch[0]["id"])]),
    "erigon": lambda batch: (200, [_error(-32600, "batch limit 100 exceeded (can increase by --rpc.batch.limit). "
                                                  f"Requested batch of size: {len(batch)}", batch[0]["id"])]),
    "jsonrpsee": lambda batch: (200, _error(-32010, "The batch request was too large")),
    "jsonrpsee response": lambda batch: (200, _error(-32011, "The batch response was too large")),
    "jsonrpsee body": lambda batch: (413, _error(-32007, "Request is too big")),
    "nethermind": lambda batch: (503, _error(-32005, "Batch size limit exceeded")),
    "besu": lambda batch: (200, _error(-32005, "Number of requests exceeds max batch size")),
    # A proxy in front of the node, refusing too large a body with its own page.
    "nginx": lambda batch: (413, NGINX_413),
}
# How each node answers the requests left once a batch's response grows too large.
CUTOFFS = {
    "geth": (-32003, "response too large"),
    "nethermind": (-32005, "MaxBatchResponseBodySize of 32768KB exceeded"),
}


@pytest.fixture
def node(monkeypatch):
    """A node taking at most `limit` requests in a batch, rejecting more with `reject`,
    or answering those past `limit` with `cutoff`; `posts` holds each batch's size."""
    monkeypatch.setenv("ETH_RPC_URL", "http://node")
    monkeypatch.setattr(ethjsonrpc, "_batch_limits", {})
    rpc = MockEthRpc()
    for i in range(250):
        rpc.set_storage(ACCOUNT, f"0x{i:064x}", f"0x{i + 1:064x}")
    node = type("Node", (), {"limit": None, "reject": None, "cutoff": None, "posts": []})()

    def post(url, json):
        node.posts.append(len(json))
        if node.limit is None or len(json) <= node.limit:
            return rpc(url, json=json)
        if node.reject is not None:
            status, body = node.reject(json)
            return FakeResponse(status, body if isinstance(body, str) else json_module.dumps(body))
        answers = json_module.loads(rpc(url, json=json[: node.limit]).text)
        answers += [_error(*node.cutoff, req["id"]) for req in json[node.limit :]]
        return FakeResponse(200, json_module.dumps(answers))

    monkeypatch.setattr(ethjsonrpc, "post", post)
    return node


def _assert_answered(answers: dict, count: int) -> None:
    assert answers == {
        i: {"jsonrpc": "2.0", "id": i, "result": f"0x{i + 1:064x}"} for i in range(count)
    }


def test_batches_carry_at_most_a_hundred_requests(node):
    _assert_answered(ethjsonrpc.post_batch(_storage_batch(250)), 250)
    assert node.posts == [100, 100, 50]


@pytest.mark.parametrize("reject", REJECTIONS.values(), ids=REJECTIONS.keys())
def test_a_batch_rejected_as_too_large_is_halved(node, reject):
    node.limit, node.reject = 30, reject
    _assert_answered(ethjsonrpc.post_batch(_storage_batch(70)), 70)
    assert node.posts == [70, 35, 17, 17, 17, 17, 2]
    node.posts.clear()
    ethjsonrpc.post_batch(_storage_batch(34))
    assert node.posts == [17, 17]  # the node's limit is remembered


@pytest.mark.parametrize("cutoff", CUTOFFS.values(), ids=CUTOFFS.keys())
def test_requests_cut_off_by_too_large_a_response_are_resent(node, cutoff):
    node.limit, node.cutoff = 30, cutoff
    _assert_answered(ethjsonrpc.post_batch(_storage_batch(70)), 70)
    assert node.posts == [70, 30, 10]


def test_a_rejected_batch_of_one_fails(node):
    node.limit, node.reject = 0, REJECTIONS["jsonrpsee"]
    with pytest.raises(ethjsonrpc.RpcError, match="rejected a batch of one request"):
        ethjsonrpc.post_batch(_storage_batch(1))


# -32005 also means a node is overloaded, or rate-limits, rather than that a batch is too large.
OVERLOADS = {
    "erigon": lambda batch: (503, _error(-32005, "server overloaded, retry later")),
    "erigon database": lambda batch: (
        503, [_error(-32005, "server overloaded, retry later", req["id"]) for req in batch]
    ),
    "rate limit": lambda batch: (429, _error(-32005, "Too many requests")),
    # As besu rejects too large a batch, but for some other limit.
    "limit over HTTP 200": lambda batch: (200, _error(-32005, "Too many requests")),
}


@pytest.mark.parametrize("overload", OVERLOADS.values(), ids=OVERLOADS.keys())
def test_an_overloaded_node_fails_the_batch_without_shrinking_it(node, overload):
    node.limit, node.reject = 30, overload
    with pytest.raises(ethjsonrpc.RpcError, match="ETH_RPC_URL: "):
        ethjsonrpc.post_batch(_storage_batch(70))
    assert node.posts == [70]


def test_requests_limited_by_other_than_size_are_answered_as_errors(node):
    node.limit, node.cutoff = 30, (-32005, "Too many requests")
    answers = ethjsonrpc.post_batch(_storage_batch(70))
    assert node.posts == [70]
    assert answers[69]["error"] == {"code": -32005, "message": "Too many requests"}


def test_rpc_batch_halves_a_batch_the_node_rejects(node):
    node.limit, node.reject = 2, REJECTIONS["nethermind"]
    calls = [("eth_getStorageAt", [ACCOUNT, f"0x{i:064x}", "0x10"]) for i in range(5)]
    assert ethjsonrpc.rpc_batch(calls) == [f"0x{i + 1:064x}" for i in range(5)]
    assert node.posts == [5, 2, 2, 1]


@pytest.mark.parametrize("text", ["<html>502 Bad Gateway</html>", "null", "[]"])
def test_a_request_answered_with_no_json_rpc_answer_fails(post, text):
    post["response"] = FakeResponse(200, text)
    with pytest.raises(ethjsonrpc.RpcError, match="eth_chainId: not a JSON-RPC answer"):
        ethjsonrpc.rpc("eth_chainId", [])


@pytest.mark.parametrize("status", [200, 503])
def test_a_batch_answered_with_no_json_fails(post, status):
    post["response"] = FakeResponse(status, "<html>Service Unavailable</html>")
    with pytest.raises(ethjsonrpc.RpcError, match=f"HTTP {status}, not JSON: <html>Service Unavailable"):
        ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])])
