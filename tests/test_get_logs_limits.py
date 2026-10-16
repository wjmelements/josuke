"""How `audit.get_logs` adapts to each client's `eth_getLogs` limits (#22).

There is no standard way to signal a limit; each client words its own error.
A range cap rejects `toBlock - fromBlock > N`, so when the message states N,
windows of N + 1 blocks fit; two clients state no N, so only halving finds
theirs. A result cap depends on how many logs a stretch of blocks holds, not on
its width, and its number is no range. Reth's names the largest range from
`fromBlock` that fits.

`get_logs` queries the whole range, then sends the windows each rejection
narrows to in batches: a batch costs one round trip however many windows it
holds, so their width hardly matters. Only rejected windows narrow, in halves,
to a stated cap's width, or as reth names, into the next batch.

A node may also lack the history asked for, which no narrower query fixes; see
"missing history" below.

Messages and codes are each client's, as of its default branch on 2026-10-12.
"""

from unittest.mock import patch

import pytest
import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from ethrpc_mock import FakeResponse, MockEthRpc, chained, connect_timeout
from josuke import audit, ethjsonrpc
from josuke.ethjsonrpc import RpcError

PROXY = "0x2222222222222222222222222222222222222222"
LIMIT = 99  # the range cap a node is configured with, or its result cap in logs
TOPIC = "0x" + "11" * 32


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    monkeypatch.setattr(ethjsonrpc, "_batch_limits", {})
    rpc = MockEthRpc()
    with patch("josuke.ethjsonrpc.post", rpc):
        yield rpc


def _log_at(node, *blocks):
    for block in blocks:
        node.add_log(PROXY, [audit.SELECTOR_DELEGATED_TOPIC, TOPIC, TOPIC], block=block)


def _range(request):
    query = request["params"][0]
    return int(query["fromBlock"], 16), int(query["toBlock"], 16)


def _scan_history(node, lo, hi):
    """The blocks of the logs `get_logs` returns, the ranges it reports missing
    from the node's history, and what it sent: a single query as its
    (fromBlock, toBlock), a batch as a list of them."""
    logs, missing = audit.get_logs(PROXY, lo, hi)
    sent = [[_range(r) for r in payload] if isinstance(payload, list) else _range(payload) for payload in node.calls]
    return [int(log["blockNumber"], 16) for log in logs], missing, sent


def _scan(node, lo, hi):
    """`_scan_history` for a node that holds all the history asked for."""
    blocks, missing, sent = _scan_history(node, lo, hi)
    assert missing == []
    return blocks, sent


def _windows(lo, hi, span):
    return [(start, min(start + span - 1, hi)) for start in range(lo, hi + 1, span)]


def _halved(windows):
    """Each of `windows` in halves, as a window that states no cap narrows."""
    return [half for a, b in windows for half in ((a, (a + b - 1) // 2), ((a + b + 1) // 2, b))]


def _range_limit(code, message, limit=LIMIT):
    """A node that rejects `to - from > limit`, as every client checks it."""

    def check(lo, hi, logs):
        if hi - lo > limit:
            return {"code": code, "message": message.format(limit=limit, given=hi - lo)}

    return check


def _result_limit(code, message, limit=LIMIT):
    """A node that rejects a multi-block query matching more than `limit` logs."""

    def check(lo, hi, logs):
        if lo != hi and len(logs) > limit:
            return {"code": code, "message": message.format(limit=limit)}

    return check


def _uses_stated_range_limit(node, code, message, limit=LIMIT, lo=0, hi=999):
    _log_at(node, lo, (lo + hi) // 2, hi)
    node.get_logs_limit = _range_limit(code, message, limit)
    blocks, sent = _scan(node, lo, hi)
    # The stated cap is known to fit: every window goes in the next batch.
    assert sent == [(lo, hi), _windows(lo, hi, limit + 1)]
    assert blocks == [lo, (lo + hi) // 2, hi]


# -- range caps that state their maximum --------------------------------------

STATED_RANGE_LIMITS = {
    "geth": (-32602, "exceed maximum block range {limit}"),
    "op-geth": (-32000, "exceed maximum block range: {limit}"),
    "erigon": (-32602, "query block range exceeds server limit, narrow your filter: {limit}"),
    "reth": (-32602, "query exceeds max block range {limit}"),
    "ethermint": (-32000, "maximum [from, to] blocks distance: {limit}"),
    # Forest words its EthErrors::BlockRangeExceeded as Lotus does.
    "lotus, forest": (-32005, "block range exceeds maximum of {limit} (got {given})"),
    # Also lotus before v1.36 and forest before v0.33, word for word.
    "venus": (1, "invalid epoch range: range between to and from blocks is too large (maximum: {limit})"),
    # Worded for queries from or up to `latest`, which josuke doesn't send; still stated caps.
    "venus from the past": (1, "invalid epoch range: from block is too far in the past (maximum: {limit})"),
    "venus into the future": (1, "invalid epoch range: to block is too far in the future (maximum: {limit})"),
}


@pytest.mark.parametrize("code, message", STATED_RANGE_LIMITS.values(), ids=STATED_RANGE_LIMITS.keys())
def test_stated_range_limit(node, code, message):
    _uses_stated_range_limit(node, code, message)


def test_bsc_range_limit_is_fixed_at_5000(node):
    _uses_stated_range_limit(node, -32000, "exceed maximum block range: {limit}", limit=5000, hi=12000)


def test_lotus_range_limit_on_glif_calibration(node):
    # The audit profiled on Glif: halving from 30,919 blocks landed on 241, for
    # 136 queries. The stated 360 admits 361-block windows: the probe, then 86 in a batch.
    _uses_stated_range_limit(
        node, -32005, "block range exceeds maximum of {limit} (got {given})", limit=360, lo=4119460, hi=4150378
    )


# -- range caps that don't ----------------------------------------------------

UNSTATED_RANGE_LIMITS = {
    "bor": (-32000, "block range exceeds configured limit"),
    "besu": (-32005, "Requested range exceeds maximum RPC range limit"),
}


@pytest.mark.parametrize("code, message", UNSTATED_RANGE_LIMITS.values(), ids=UNSTATED_RANGE_LIMITS.keys())
def test_unstated_range_limit_halves(node, code, message):
    _log_at(node, 0, 500, 999)
    node.get_logs_limit = _range_limit(code, message)
    blocks, sent = _scan(node, 0, 999)
    # Every window halves at once: 1000 blocks to 500, 250, 125, then 62 or 63, which fit.
    rounds = [[(0, 999)]]
    while len(rounds) < 5:
        rounds.append(_halved(rounds[-1]))
    assert sent == [(0, 999), *rounds[1:]]
    assert blocks == [0, 500, 999]


# -- result caps --------------------------------------------------------------


def test_reth_result_limit_names_the_range_to_retry(node):
    """Reth stops at the first block whose logs overflow the cap and names the
    range before it, the most that fits from `fromBlock`. That range is known to
    fit, and the rest of the rejected window splits into windows of its width."""
    _log_at(node, *range(200, 260))

    def reth(lo, hi, logs):
        if lo != hi and len(logs) > 10:
            overflow = int(logs[10]["blockNumber"], 16)
            retry_to = lo if overflow == lo else overflow - 1
            return {"code": -32602, "message": f"query exceeds max results 10, retry with the range {lo}-{retry_to}"}

    node.get_logs_limit = reth
    blocks, sent = _scan(node, 0, 999)
    assert blocks == list(range(200, 260))
    assert sent == [
        (0, 999),
        [(0, 209), *_windows(210, 999, 210)],
        [(210, 219), *_windows(220, 419, 10)],
    ]


RESULT_LIMITS = {
    "nethermind": (-32005, "Too many logs requested. Max logs per response is {limit}."),
    "erigon": (-32602, "query returns too many logs, narrow your filter: {limit}"),
    "ethermint": (-32000, "query returned more than {limit} results"),
    "lotus": (1, "filter matches too many events, try a more restricted filter"),
    "forest": (-32603, "filter matches too many events across multiple tipsets (maximum {limit}); narrow the block range"),
    "venus": (1, "too many events"),
}


@pytest.mark.parametrize("code, message", RESULT_LIMITS.values(), ids=RESULT_LIMITS.keys())
def test_result_limit_splits_windows(node, code, message):
    """Blocks 200-399 hold a log each, twice the cap; the rest hold none."""
    _log_at(node, *range(200, 400))
    node.get_logs_limit = _result_limit(code, message)
    blocks, sent = _scan(node, 0, 9999)
    assert blocks == list(range(200, 400))
    # Halved, never cut to the cap's number; only the half holding the dense
    # stretch halves again, until (0, 155) and (312, 624) fit, and (156, 311),
    # with 112 logs, splits once more.
    assert sent == [
        (0, 9999),
        [(0, 4999), (5000, 9999)],
        [(0, 2499), (2500, 4999)],
        [(0, 1249), (1250, 2499)],
        [(0, 624), (625, 1249)],
        [(0, 311), (312, 624)],
        [(0, 155), (156, 311)],
        [(156, 233), (234, 311)],
    ]


# -- without limits, and other errors -----------------------------------------


def test_one_query_when_nothing_is_rejected(node):
    _log_at(node, 0, 100)
    assert _scan(node, 0, 100) == ([0, 100], [(0, 100)])


def test_unrecognised_error_halves(node):
    _log_at(node, 0, 30, 99)
    node.get_logs_limit = lambda lo, hi, logs: {"code": -32000, "message": "too many results"} if hi - lo >= 25 else None
    assert _scan(node, 0, 99) == ([0, 30, 99], [(0, 99), [(0, 49), (50, 99)], _windows(0, 99, 25)])


def test_http_failure_halves(node):
    # A gateway timing out on a wide query answers with an HTTP error, not JSON-RPC.
    _log_at(node, 0, 30)
    answer = node.__call__

    def gateway(url, *args, **kwargs):
        query = kwargs["json"]
        if isinstance(query, dict) and _range(query)[1] - _range(query)[0] >= 25:
            node.calls.append(query)
            return FakeResponse(504, "Gateway Timeout")
        return answer(url, *args, **kwargs)

    with patch("josuke.ethjsonrpc.post", gateway):
        # A 504 is resent once before the query narrows.
        assert _scan(node, 0, 49) == ([0, 30], [(0, 49), (0, 49), [(0, 24), (25, 49)]])


# How requests fails a POST the node took but didn't answer in full: a timeout or a reset
# while reading the body. Each is a ConnectionError, not a Timeout.
DROPPED = {
    "body late": lambda: chained(
        requests.ConnectionError("read timed out"), ReadTimeoutError(None, "", "read timed out"), TimeoutError()
    ),
    "reset": lambda reset=ConnectionResetError(54, "Connection reset by peer"): chained(
        requests.exceptions.ChunkedEncodingError("connection broken"), ProtocolError("Connection broken", reset), reset
    ),
}


@pytest.mark.parametrize("failure", DROPPED.values(), ids=DROPPED.keys())
def test_dropped_query_halves(node, failure):
    _log_at(node, 0, 30)
    with _failing_over_http(node, failure, lambda ranges: ranges[0][1] - ranges[0][0] >= 25):
        assert _scan(node, 0, 49) == ([0, 30], [(0, 49), [(0, 24), (25, 49)]])


def test_node_too_slow_to_connect_to_is_not_narrowed(node):
    # requests' ConnectTimeout is a Timeout, but no query was sent, so a narrower one can't help.
    with _failing_over_http(node, connect_timeout, lambda ranges: True):
        with pytest.raises(RpcError, match="cannot reach the node: timed out"):
            audit.get_logs(PROXY, 0, 999)
    assert len(node.calls) == 1


def _failing_over_http(node, status, fails):
    """`node` behind a gateway answering `status` to any POST `fails` picks out by its
    queries' (fromBlock, toBlock)s; `status` may instead make the error `post` raises."""
    answer = node.__call__

    def gateway(url, *args, **kwargs):
        payload = kwargs["json"]
        ranges = [_range(r) for r in payload] if isinstance(payload, list) else [_range(payload)]
        if fails(ranges):
            node.calls.append(payload)
            if callable(status):
                raise status()
            return FakeResponse(status, "")
        return answer(url, *args, **kwargs)

    return patch("josuke.ethjsonrpc.post", gateway)


def test_batch_http_failure_narrows(node):
    # Windows of the stated cap time out at the gateway as a batch; halves don't.
    _log_at(node, 0, 450, 999)
    node.get_logs_limit = _range_limit(-32602, "exceed maximum block range {limit}")
    with _failing_over_http(node, 504, lambda ranges: len(ranges) > 1 and ranges[0][1] - ranges[0][0] >= 50):
        blocks, sent = _scan(node, 0, 999)
    assert blocks == [0, 450, 999]
    assert sent == [(0, 999), _windows(0, 999, 100), _windows(0, 999, 100), _windows(0, 999, 50)]


def test_batch_http_failure_keeps_single_block_windows(node):
    # The tail of 0-1000 in windows of 100 is one block, which can't narrow; the
    # batch's failure isn't its own, so it rides along while the rest halve.
    _log_at(node, 0, 450, 1000)
    node.get_logs_limit = _range_limit(-32602, "exceed maximum block range {limit}")
    with _failing_over_http(node, 504, lambda ranges: len(ranges) > 1 and ranges[0][1] - ranges[0][0] >= 50):
        blocks, sent = _scan(node, 0, 1000)
    assert blocks == [0, 450, 1000]
    tail = [(1000, 1000)]
    assert sent == [(0, 1000), _windows(0, 999, 100) + tail, _windows(0, 999, 100) + tail, _windows(0, 999, 50) + tail]


def test_batch_http_failure_of_single_blocks_propagates(node):
    node.get_logs_limit = _range_limit(-32602, "exceed maximum block range {limit}", 0)
    with _failing_over_http(node, 504, lambda ranges: len(ranges) > 1):
        with pytest.raises(RpcError, match="HTTP 504"):
            audit.get_logs(PROXY, 0, 9)
    assert len(node.calls) == 1 + 2  # the probe, then the batch of single blocks and its resend


@pytest.mark.parametrize("batched", [False, True])
def test_rate_limit_is_not_narrowed(node, batched):
    # Narrower queries are only more queries.
    node.get_logs_limit = _range_limit(-32602, "exceed maximum block range {limit}")
    with _failing_over_http(node, 429, lambda ranges: (len(ranges) > 1) == batched):
        with pytest.raises(RpcError, match="HTTP 429"):
            audit.get_logs(PROXY, 0, 999)
    assert len(node.calls) == 5 + batched


def test_single_block_failure_propagates(node):
    node.get_logs_limit = lambda lo, hi, logs: {"code": -32000, "message": "node error"}
    with pytest.raises(RpcError, match="block 5"):
        audit.get_logs(PROXY, 5, 5)


# Errors that sound like, or come from the same code as, missing history but aren't it.
NOT_MISSING_HISTORY = [
    # geth's log filter: a block above the node's head
    (-32000, "header not found"),
    # lotus-gateway: checkEpoch's other refusal, for a block above the head
    (1, "bad tipset height: tipset height in future"),
    # lotus: the not-indexed error's prefix, failing for some other reason
    (1, "failed to get events for filter: failed to get events for filter from chain indexer: context deadline exceeded"),
    # a provider's rate limit
    (-32005, "Too many requests"),
]


@pytest.mark.parametrize("code, message", NOT_MISSING_HISTORY)
def test_error_not_missing_history(node, code, message):
    node.get_logs_limit = lambda lo, hi, logs: {"code": code, "message": message}
    with pytest.raises(RpcError, match="block 5"):
        audit.get_logs(PROXY, 5, 5)


BAD_RESPONSES = [{}, {"result": None}, {"result": {}}, {"result": "0x"}]


def _answer_badly(node, monkeypatch, bad, when):
    """Answer the eth_getLogs queries `when` picks out by range with `bad`, which has
    no error and no list of logs."""
    dispatch = node._dispatch

    def bad_dispatch(req):
        if req["method"] == "eth_getLogs" and when(*_range(req)):
            return {"jsonrpc": "2.0", "id": req.get("id"), **bad}
        return dispatch(req)

    monkeypatch.setattr(node, "_dispatch", bad_dispatch)


@pytest.mark.parametrize("bad", BAD_RESPONSES)
def test_bad_response_is_fatal(node, monkeypatch, bad):
    _answer_badly(node, monkeypatch, bad, lambda lo, hi: True)
    with pytest.raises(RpcError, match="eth_getLogs blocks 0-99: bad response"):
        audit.get_logs(PROXY, 0, 99)
    assert len(node.calls) == 1  # no narrower query fixes it


@pytest.mark.parametrize("bad", BAD_RESPONSES)
def test_bad_response_in_a_batch_is_fatal(node, monkeypatch, bad):
    node.get_logs_limit = _range_limit(-32602, "exceed maximum block range {limit}")
    _answer_badly(node, monkeypatch, bad, lambda lo, hi: lo == 200)
    with pytest.raises(RpcError, match="eth_getLogs blocks 200-299: bad response"):
        audit.get_logs(PROXY, 0, 999)
    assert len(node.calls) == 2


def test_sorts_by_block_then_index(node):
    for block, index in [(2, 0), (1, 1), (1, 0)]:
        node.add_log(PROXY, [audit.SELECTOR_DELEGATED_TOPIC, TOPIC, TOPIC], block=block, log_index=index)
    logs, missing = audit.get_logs(PROXY, 0, 10)
    assert [(int(log["blockNumber"], 16), int(log["logIndex"], 16)) for log in logs] == [(1, 0), (1, 1), (2, 0)]
    assert missing == []


# -- missing history -----------------------------------------------------------
#
# A node that pruned its oldest blocks, or never indexed their events, can't
# serve them at any width, so narrowing doesn't help. History is missing from
# the start, so `get_logs` finds the first block the node holds, reports the
# blocks before it missing, and scans the rest. Single-block probes find it:
# they fit any range or result cap.

CUTOFF = 600  # the node holds blocks from here on


def _history_from(code, message, cutoff=CUTOFF):
    """A node without blocks before `cutoff`: a query reaching below it fails,
    as geth's `fromBlock < HistoryPruningCutoff` does."""

    def check(lo, hi, logs):
        if lo < cutoff:
            return {"code": code, "message": message.format(lo=lo, cutoff=cutoff)}

    return check


# Bisecting 0-999 with single blocks for the first that the node holds, 600:
# 999 is held, so history isn't missing throughout; then halve the gap.
BISECT_TO_CUTOFF = [(b, b) for b in (999, 499, 749, 624, 561, 592, 608, 600, 596, 598, 599)]


MISSING_HISTORY = {
    # Also op-geth, bor, bsc: the EIP-4444 code.
    "geth, reth": (4444, "pruned history unavailable"),
    "nethermind, besu": (4444, "Pruned history unavailable"),
    # A client may word EIP-4444's error its own way.
    "code alone": (4444, "history expired"),
    # A proxy may replace the code and keep only the message.
    "words alone": (-32000, "pruned history unavailable"),
    "lotus events not indexed": (1, "failed to get events for filter: failed to get events for filter from chain indexer: not found in index"),
    "lotus index requires backfill": (1, "failed to get events for filter: failed to get events for filter from chain indexer: chain index requires backfill"),
    "lotus-gateway lookback": (1, "bad tipset height: lookbacks of more than 24h0m0s are disallowed"),
}


@pytest.mark.parametrize("code, message", MISSING_HISTORY.values(), ids=MISSING_HISTORY.keys())
def test_missing_history(node, code, message):
    _log_at(node, 100, 700, 999)
    node.get_logs_limit = _history_from(code, message)
    blocks, missing, sent = _scan_history(node, 0, 999)
    assert missing == [(0, CUTOFF - 1)]
    assert blocks == [700, 999]
    assert sent == [(0, 999), *BISECT_TO_CUTOFF, (CUTOFF, 999)]


def test_lotus_gateway_lookback_under_its_range_limit(node):
    """Lotus-gateway's defaults: a 360-epoch range cap, checked first, and a
    24-hour lookback, 2880 epochs at 30 seconds each."""
    lo, hi = 0, 9999
    cutoff = hi + 1 - 2880
    _log_at(node, 100, 8000)
    range_limit = _range_limit(-32005, "block range exceeds maximum of {limit} (got {given})", 360)
    lookback = _history_from(1, "bad tipset height: lookbacks of more than 24h0m0s are disallowed", cutoff)
    node.get_logs_limit = lambda a, b, logs: range_limit(a, b, logs) or lookback(a, b, logs)
    blocks, missing, sent = _scan_history(node, lo, hi)
    assert missing == [(lo, cutoff - 1)]
    assert blocks == [8000]
    assert sent[:2] == [(lo, hi), _windows(lo, hi, 361)]


def test_erigon_pruned_history_names_its_first_block(node):
    # Erigon says where its history starts, so there is nothing to bisect.
    _log_at(node, 100, 700, 999)
    node.get_logs_limit = _history_from(
        -32000, "old data not available due to pruning: requested block {lo}, blocks are available from block {cutoff}"
    )
    blocks, missing, sent = _scan_history(node, 0, 999)
    assert missing == [(0, CUTOFF - 1)]
    assert blocks == [700, 999]
    assert sent == [(0, 999), (CUTOFF, 999)]


def test_history_missing_throughout(node):
    _log_at(node, 100)
    node.get_logs_limit = _history_from(4444, "pruned history unavailable", cutoff=1000)
    blocks, missing, sent = _scan_history(node, 0, 999)
    assert (blocks, missing) == ([], [(0, 999)])
    assert sent == [(0, 999), (999, 999)]


def test_missing_history_under_a_range_limit(node):
    """Glif's Lotus: a 360-block range cap, and events indexed from 4144279."""
    lo, hi, cutoff = 4119460, 4150774, 4144279
    _log_at(node, 4119464, 4150000)
    range_limit = _range_limit(-32005, "block range exceeds maximum of {limit} (got {given})", 360)
    history = _history_from(1, "failed to get events for filter: not found in index", cutoff)
    node.get_logs_limit = lambda a, b, logs: range_limit(a, b, logs) or history(a, b, logs)
    blocks, missing, sent = _scan_history(node, lo, hi)
    assert missing == [(lo, cutoff - 1)]
    assert blocks == [4150000]
    # The stated cap batches every window at once; those from the start up to
    # 4144008-4144368 fail for their history. Single blocks bisect only that
    # last one, and the part of it the node holds goes in the next batch.
    assert sent[:2] == [(lo, hi), _windows(lo, hi, 361)]
    probes = sent[2:-1]
    assert all(isinstance(p, tuple) and p[0] == p[1] and 4144008 <= p[0] <= 4144368 for p in probes)
    assert len(probes) <= 10
    assert sent[-1] == (cutoff, 4144368)


def test_missing_history_in_a_batched_window_is_reported_whole(node):
    # A gap in the middle: the window that hits it is reported, the rest scanned.
    _log_at(node, 0, 450, 999)
    gap = lambda lo, hi, logs: {"code": 4444, "message": "pruned history unavailable"} if lo <= 450 <= hi else None
    range_limit = _range_limit(-32602, "exceed maximum block range {limit}")
    node.get_logs_limit = lambda a, b, logs: range_limit(a, b, logs) or gap(a, b, logs)
    blocks, missing, sent = _scan_history(node, 0, 999)
    assert missing == [(400, 499)]
    assert blocks == [0, 999]
    assert sent == [(0, 999), _windows(0, 999, 100)]


def test_a_probe_finding_history_too_low_is_sought_past(node):
    """A Lotus null round has no tipset, so its events count as indexed even where
    history is missing: probing one finds the history's start too low. The window
    from there fails for its history again and is bisected again, not reported
    whole as a gap."""
    cutoff, null_round = 650, 649
    _log_at(node, 100, 660, 999)
    range_limit = _range_limit(-32602, "exceed maximum block range {limit}")
    history = _history_from(1, "failed to get events for filter: not found in index", cutoff)
    lotus = lambda a, b, logs: None if a == b == null_round else history(a, b, logs)
    node.get_logs_limit = lambda a, b, logs: range_limit(a, b, logs) or lotus(a, b, logs)
    blocks, missing, sent = _scan_history(node, 0, 999)
    assert missing == [(0, cutoff - 1)]
    assert blocks == [660, 999]
    assert sent[-1] == (cutoff, 699)


def test_a_probe_failing_over_http_fails(node):
    # Taking it for a block held would report too little history missing.
    _log_at(node, 100, 700, 999)
    node.get_logs_limit = _history_from(4444, "pruned history unavailable")
    with _failing_over_http(node, 429, lambda ranges: ranges[0][0] == ranges[0][1]):
        with pytest.raises(RpcError, match="HTTP 429"):
            audit.get_logs(PROXY, 0, 999)


def test_a_probe_failing_otherwise_fails(node):
    _log_at(node, 100, 700, 999)
    history = _history_from(4444, "pruned history unavailable")
    rate_limit = {"code": -32005, "message": "Too many requests"}
    node.get_logs_limit = lambda lo, hi, logs: rate_limit if lo == hi else history(lo, hi, logs)
    with pytest.raises(RpcError, match="block 999: .*Too many requests"):
        audit.get_logs(PROXY, 0, 999)
