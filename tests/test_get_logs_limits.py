"""How `audit.get_logs` adapts to each client's `eth_getLogs` limits (#22).

There is no standard way to signal a limit; each client words its own error.
A range cap rejects `toBlock - fromBlock > N`, so when the message states N,
windows of N + 1 blocks fit; two clients state no N, so only halving finds
theirs. A result cap depends on how many logs a stretch of blocks holds, not on
its width, and its number is no range. Reth's names the largest range from
`fromBlock` that fits.

`get_logs` probes one query at a time until one succeeds or a range is known to
fit, then sends the remaining windows in batches: a batch costs one round trip
however many windows it holds, so their width hardly matters. A window rejected
for its results splits, in halves or as reth names, into the next batch.

Messages and codes are each client's, as of its default branch on 2026-10-12.
"""

from unittest.mock import patch

import pytest

from ethrpc_mock import MockEthRpc
from josuke import audit, ethjsonrpc

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


def _scan(node, lo, hi):
    """The blocks of the logs `get_logs` returns, and what it sent: a single query
    as its (fromBlock, toBlock), a batch as a list of them."""
    logs = audit.get_logs(PROXY, lo, hi)
    sent = [[_range(r) for r in payload] if isinstance(payload, list) else _range(payload) for payload in node.calls]
    return [int(log["blockNumber"], 16) for log in logs], sent


def _windows(lo, hi, span):
    return [(start, min(start + span - 1, hi)) for start in range(lo, hi + 1, span)]


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


def _halves_to_unstated_range_limit(node, code, message):
    _log_at(node, 0, 500, 999)
    node.get_logs_limit = _range_limit(code, message)
    blocks, sent = _scan(node, 0, 999)
    # 1000 blocks halve to 500, 250, 125, then 62, the first that fits.
    assert sent == [(0, 999), (0, 499), (0, 249), (0, 124), (0, 61), _windows(62, 999, 62)]
    assert blocks == [0, 500, 999]


def _splits_windows_over_result_limit(node, code, message):
    """Blocks 200-399 hold a log each, twice the cap; the rest hold none."""
    _log_at(node, *range(200, 400))
    node.get_logs_limit = _result_limit(code, message)
    blocks, sent = _scan(node, 0, 9999)
    assert blocks == list(range(200, 400))
    # Halved, never cut to the cap's number, until (0, 155) fits; then windows of
    # 156 blocks, of which (156, 311) holds 112 logs and splits in two.
    assert sent == [
        (0, 9999), (0, 4999), (0, 2499), (0, 1249), (0, 624), (0, 311), (0, 155),
        _windows(156, 9999, 156),
        [(156, 233), (234, 311)],
    ]


# -- range caps that state their maximum --------------------------------------


def test_geth_range_limit(node):
    _uses_stated_range_limit(node, -32602, "exceed maximum block range {limit}")


def test_op_geth_range_limit(node):
    _uses_stated_range_limit(node, -32000, "exceed maximum block range: {limit}")


def test_bsc_range_limit_is_fixed_at_5000(node):
    _uses_stated_range_limit(node, -32000, "exceed maximum block range: {limit}", limit=5000, hi=12000)


def test_erigon_range_limit(node):
    _uses_stated_range_limit(node, -32602, "query block range exceeds server limit, narrow your filter: {limit}")


def test_reth_range_limit(node):
    _uses_stated_range_limit(node, -32602, "query exceeds max block range {limit}")


def test_ethermint_range_limit(node):
    _uses_stated_range_limit(node, -32000, "maximum [from, to] blocks distance: {limit}")


def test_lotus_range_limit(node):
    _uses_stated_range_limit(node, -32005, "block range exceeds maximum of {limit} (got {given})")


def test_lotus_range_limit_on_glif_calibration(node):
    # The audit profiled on Glif: halving from 30,919 blocks landed on 241, for
    # 136 queries. The stated 360 admits 361-block windows: the probe, then 86 in a batch.
    _uses_stated_range_limit(
        node, -32005, "block range exceeds maximum of {limit} (got {given})", limit=360, lo=4119460, hi=4150378
    )


def test_forest_range_limit(node):
    # Forest words its EthErrors::BlockRangeExceeded as Lotus does.
    _uses_stated_range_limit(node, -32005, "block range exceeds maximum of {limit} (got {given})")


def test_venus_range_limit(node):
    _uses_stated_range_limit(
        node, 1, "invalid epoch range: range between to and from blocks is too large (maximum: {limit})"
    )


def test_venus_range_limit_from_the_past(node):
    # Worded for a query up to `latest`, which josuke doesn't send; still a stated cap.
    _uses_stated_range_limit(node, 1, "invalid epoch range: from block is too far in the past (maximum: {limit})")


def test_venus_range_limit_into_the_future(node):
    _uses_stated_range_limit(node, 1, "invalid epoch range: to block is too far in the future (maximum: {limit})")


# -- range caps that don't ----------------------------------------------------


def test_bor_range_limit_states_no_maximum(node):
    _halves_to_unstated_range_limit(node, -32000, "block range exceeds configured limit")


def test_besu_range_limit_states_no_maximum(node):
    _halves_to_unstated_range_limit(node, -32005, "Requested range exceeds maximum RPC range limit")


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


def test_nethermind_result_limit(node):
    _splits_windows_over_result_limit(node, -32005, "Too many logs requested. Max logs per response is {limit}.")


def test_erigon_result_limit(node):
    _splits_windows_over_result_limit(node, -32602, "query returns too many logs, narrow your filter: {limit}")


def test_ethermint_result_limit(node):
    _splits_windows_over_result_limit(node, -32000, "query returned more than {limit} results")


def test_lotus_result_limit(node):
    _splits_windows_over_result_limit(node, 1, "filter matches too many events, try a more restricted filter")


def test_forest_result_limit(node):
    _splits_windows_over_result_limit(
        node,
        -32603,
        "filter matches too many events across multiple tipsets (maximum {limit}); narrow the block range",
    )


def test_venus_result_limit(node):
    _splits_windows_over_result_limit(node, 1, "too many events")
