"""Tests for josuke.ethjsonrpc's batches, with the HTTP layer faked."""

import json as json_module
import socket
import struct
import threading

import click
import pytest
import requests

from ethrpc_mock import FakeResponse, MockEthRpc, connect_timeout
from josuke import ethjsonrpc

ACCOUNT = "0x" + "aa" * 20


class Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json_module.dumps(body)
        self.headers = {}

    def json(self):
        return self._body


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    sent = {}

    def fake_post(url, json, timeout):
        sent["payload"], sent["timeout"] = json, timeout
        if isinstance(sent["response"], Exception):
            raise sent["response"]
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
    post["response"] = Response(None, status_code=500)
    with pytest.raises(ethjsonrpc.RpcError, match="HTTP 500") as failure:
        ethjsonrpc.rpc_batch([("eth_chainId", [])])
    assert failure.value.status == 500


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

    def post(url, json, timeout):
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


# -32005 also means a node is overloaded, or rate-limits, rather than that a batch is too
# large; each overload with the number of times the batch is sent before failing.
OVERLOADS = {
    "erigon": (lambda batch: (503, _error(-32005, "server overloaded, retry later")), 5),
    "erigon database": (
        lambda batch: (503, [_error(-32005, "server overloaded, retry later", req["id"]) for req in batch]),
        5,
    ),
    "rate limit": (lambda batch: (429, _error(-32005, "Too many requests")), 5),
    # As besu rejects too large a batch, but for some other limit: not resent.
    "limit over HTTP 200": (lambda batch: (200, _error(-32005, "Too many requests")), 1),
}


@pytest.mark.parametrize("overload, tries", OVERLOADS.values(), ids=OVERLOADS.keys())
def test_an_overloaded_node_fails_the_batch_after_resends_without_shrinking_it(node, overload, tries, sleeps):
    node.limit, node.reject = 30, overload
    with pytest.raises(ethjsonrpc.RpcError, match="ETH_RPC_URL: "):
        ethjsonrpc.post_batch(_storage_batch(70))
    assert node.posts == [70] * tries
    assert len(sleeps) == tries - 1


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


def test_a_batch_answered_with_no_json_fails(post):
    post["response"] = FakeResponse(200, "<html>Service Unavailable</html>")
    with pytest.raises(ethjsonrpc.RpcError, match="HTTP 200, not JSON: <html>Service Unavailable"):
        ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])])


def test_a_batch_answered_with_a_proxy_page_fails_after_resends(post, sleeps):
    post["response"] = FakeResponse(503, "<html>Service Unavailable</html>")
    with pytest.raises(ethjsonrpc.RpcError, match="HTTP 503: <html>Service Unavailable"):
        ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])])
    assert len(sleeps) == 4


# -- reaching the node ---------------------------------------------------------


def test_requests_wait_foundrys_default_timeout(post):
    post["response"] = Response({"id": 1, "result": "0x1"})
    ethjsonrpc.rpc("eth_chainId", [])
    assert post["timeout"] == 45


def test_eth_rpc_timeout_sets_the_wait(post, monkeypatch):
    monkeypatch.setenv("ETH_RPC_TIMEOUT", "2.5")
    post["response"] = Response([{"id": 0, "result": "0x1"}])
    ethjsonrpc.rpc_batch([("eth_chainId", [])])
    assert post["timeout"] == 2.5


@pytest.mark.parametrize("value", ["soon", "0", "-1", "inf", "nan"])
def test_eth_rpc_timeout_must_be_a_number_of_seconds(post, monkeypatch, value):
    monkeypatch.setenv("ETH_RPC_TIMEOUT", value)
    with pytest.raises(click.ClickException, match=f"ETH_RPC_TIMEOUT: not a number of seconds: {value}"):
        ethjsonrpc.rpc("eth_chainId", [])


@pytest.fixture
def listener(monkeypatch):
    """A local socket that takes connections and never answers; its url is ETH_RPC_URL,
    with a path standing in for an API key."""
    monkeypatch.setenv("ETH_RPC_TIMEOUT", "0.05")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        monkeypatch.setenv("ETH_RPC_URL", f"http://127.0.0.1:{sock.getsockname()[1]}/v2/SECRET")
        yield sock


CALLS = {"rpc": lambda: ethjsonrpc.rpc("eth_chainId", []), "batch": lambda: ethjsonrpc.rpc_batch([("eth_chainId", [])])}


@pytest.mark.timeout(2)
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_a_node_too_slow_to_answer_fails(listener, call):
    with pytest.raises(ethjsonrpc.RpcTimeout) as failure:
        call()
    assert failure.value.message == "ETH_RPC_URL: no answer within 0.05s (ETH_RPC_TIMEOUT)"


@pytest.mark.timeout(2)
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_an_unreachable_node_fails_with_the_socket_error_and_not_the_url(listener, call):
    listener.close()  # nothing listens there now
    with pytest.raises(ethjsonrpc.RpcError) as failure:
        call()
    assert failure.value.message.startswith("ETH_RPC_URL: cannot reach the node: [Errno ")
    assert "Connection refused" in failure.value.message and "SECRET" not in failure.value.message


HEAD = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"


def _read_request(conn):
    """Read all of a request, so the socket closes cleanly: Linux resets a connection
    closed with data unread."""
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(65536)
    head, body = data.split(b"\r\n\r\n", 1)
    fields = dict(line.lower().split(b":", 1) for line in head.split(b"\r\n")[1:])
    length = int(fields[b"content-length"])
    while len(body) < length:
        body += conn.recv(65536)


def _reset(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close sends RST


# What the node does with a request it took, and how that fails.
FAILURES = {
    "headers late": ([], ethjsonrpc.RpcTimeout, "no answer within 0.05s"),
    # requests raises a ConnectionError, not a Timeout, for these
    "body late": ([lambda conn: conn.sendall(HEAD + b'{"re')], ethjsonrpc.RpcTimeout, "no answer within 0.05s"),
    "reset": ([_reset], ethjsonrpc.RpcDropped, "Connection reset"),
    "reset in body": ([lambda conn: conn.sendall(HEAD + b'{"re'), _reset], ethjsonrpc.RpcDropped, "Connection reset"),
    "closed in body": ([lambda conn: conn.sendall(HEAD + b'{"re')], ethjsonrpc.RpcDropped, "IncompleteRead"),
}


@pytest.mark.timeout(2)
@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
@pytest.mark.parametrize("steps, error, words", FAILURES.values(), ids=FAILURES.keys())
def test_a_node_failing_a_request_it_took_fails(listener, call, steps, error, words):
    failed = threading.Event()

    def node():
        conn, _ = listener.accept()
        with conn:
            _read_request(conn)
            for step in steps:
                step(conn)
            if error is ethjsonrpc.RpcTimeout:
                failed.wait(1)  # a late answer outlasts ETH_RPC_TIMEOUT

    thread = threading.Thread(target=node)
    thread.start()
    with pytest.raises(error) as failure:
        call()
    failed.set()
    thread.join()
    assert type(failure.value) is error
    assert words in failure.value.message and "SECRET" not in failure.value.message


def test_a_node_too_slow_to_connect_to_cannot_be_reached(post):
    post["response"] = connect_timeout()
    with pytest.raises(ethjsonrpc.RpcError) as failure:
        ethjsonrpc.rpc("eth_chainId", [])
    assert type(failure.value) is ethjsonrpc.RpcError
    assert failure.value.message == "ETH_RPC_URL: cannot reach the node: timed out"


def test_a_url_requests_cannot_post_to_fails(post):
    post["response"] = requests.exceptions.InvalidSchema("No connection adapters were found for 'ws://node/SECRET'")
    with pytest.raises(ethjsonrpc.RpcError) as failure:
        ethjsonrpc.rpc("eth_chainId", [])
    assert failure.value.message == "ETH_RPC_URL: not an http(s) URL"


# -- eth_getCode cache ---------------------------------------------------------

CONTRACT = "0x" + "c1" * 20
OTHER = "0x" + "c2" * 20
NOTHING = "0x" + "c3" * 20  # no code


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://node")
    monkeypatch.setattr(ethjsonrpc, "_batch_limits", {})
    rpc = MockEthRpc()
    rpc.set_code(CONTRACT, "0x60")
    rpc.set_code(OTHER, "0x61")
    monkeypatch.setattr(ethjsonrpc, "post", rpc)
    return rpc


def _code_requests(rpc):
    return [r["params"] for r in rpc.requests_for("eth_getCode")]


def test_prefetched_code_is_read_without_another_request(chain):
    ethjsonrpc.prefetch_code([CONTRACT, OTHER, CONTRACT.upper().replace("0X", "0x"), None])
    assert len(chain.calls) == 1  # one batch, each address once
    assert sorted(p[0].lower() for p in _code_requests(chain)) == [CONTRACT, OTHER]
    assert ethjsonrpc.eth_get_code(CONTRACT) == "0x60"
    assert ethjsonrpc.eth_get_code(OTHER) == "0x61"
    assert len(chain.calls) == 1


def test_code_is_read_once_per_address(chain):
    assert ethjsonrpc.eth_get_code(CONTRACT) == ethjsonrpc.eth_get_code(CONTRACT) == "0x60"
    assert len(_code_requests(chain)) == 1


def test_prefetch_skips_code_already_read(chain):
    ethjsonrpc.eth_get_code(CONTRACT)
    ethjsonrpc.prefetch_code([CONTRACT])
    assert len(chain.calls) == 1


def test_empty_code_is_read_again(chain):
    # A contract may be deployed there later in the run.
    ethjsonrpc.prefetch_code([NOTHING])
    assert ethjsonrpc.eth_get_code(NOTHING) == "0x"
    chain.set_code(NOTHING, "0x62")
    assert ethjsonrpc.eth_get_code(NOTHING) == "0x62"
    assert len(_code_requests(chain)) == 3


def test_code_at_a_block_is_not_cached(chain):
    ethjsonrpc.eth_get_code(CONTRACT, "0x5")
    ethjsonrpc.eth_get_code(CONTRACT, "0x5")
    assert _code_requests(chain) == [[CONTRACT, "0x5"], [CONTRACT, "0x5"]]


# -- resending what a node can't serve now ------------------------------------


@pytest.fixture
def answers(monkeypatch):
    """Answer each POST with the next of the responses appended to the list returned."""
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    monkeypatch.setattr(ethjsonrpc, "_batch_limits", {})
    queued = []
    monkeypatch.setattr(ethjsonrpc, "post", lambda url, json, timeout: queued.pop(0))
    return queued


def _chain_id(id=1):
    return FakeResponse(200, json_module.dumps({"jsonrpc": "2.0", "id": id, "result": "0x13a"}))


def test_a_rate_limited_request_waits_as_retry_after_asks(answers, sleeps):
    answers += [FakeResponse(429, "Too Many Requests", {"Retry-After": "3"}), _chain_id()]
    assert ethjsonrpc.rpc("eth_chainId", []) == "0x13a"
    assert sleeps == [3.0]


def test_retry_after_is_capped(answers, sleeps):
    answers += [FakeResponse(429, "Too Many Requests", {"Retry-After": "3600"}), _chain_id()]
    ethjsonrpc.rpc("eth_chainId", [])
    assert sleeps == [ethjsonrpc._MAX_WAIT]


def test_backoff_doubles_with_jitter(answers, sleeps):
    # Without Retry-After, or with an HTTP date, the wait about doubles each time.
    unavailable = FakeResponse(503, "Service Unavailable", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    answers += [unavailable, FakeResponse(502, "Bad Gateway"), FakeResponse(503, "Service Unavailable"), _chain_id()]
    assert ethjsonrpc.rpc("eth_chainId", []) == "0x13a"
    assert [base / 2 <= wait <= base for wait, base in zip(sleeps, [1, 2, 4])] == [True] * 3


@pytest.mark.parametrize("status, tries", [(429, 5), (502, 5), (503, 5), (504, 2), (500, 1)])
def test_a_request_fails_after_its_tries(answers, sleeps, status, tries):
    # A 504 may mean the request is too heavy to answer in time; the caller may lighten it.
    answers += [FakeResponse(status, "")] * 6
    with pytest.raises(ethjsonrpc.RpcError, match=f"eth_chainId: HTTP {status}") as failure:
        ethjsonrpc.rpc("eth_chainId", [])
    assert failure.value.status == status
    assert len(answers) == 6 - tries
    assert len(sleeps) == tries - 1


def test_a_rate_limited_batch_is_resent_whole(answers, sleeps):
    batch = [FakeResponse(200, json_module.dumps([{"jsonrpc": "2.0", "id": i, "result": hex(i)} for i in range(2)]))]
    answers += [FakeResponse(429, "Too Many Requests", {"Retry-After": "1"}), FakeResponse(503, ""), *batch]
    assert ethjsonrpc.rpc_batch([("eth_chainId", []), ("eth_blockNumber", [])]) == ["0x0", "0x1"]
    assert sleeps[0] == 1.0 and len(sleeps) == 2
    assert ethjsonrpc._batch_limits == {}  # not taken for too large a batch


def test_a_batch_fails_after_its_tries(answers, sleeps):
    answers += [FakeResponse(504, "Gateway Timeout")] * 3
    with pytest.raises(ethjsonrpc.RpcError, match="ETH_RPC_URL: HTTP 504: Gateway Timeout") as failure:
        ethjsonrpc.rpc_batch([("eth_chainId", [])])
    assert failure.value.status == 504
    assert len(answers) == 1

