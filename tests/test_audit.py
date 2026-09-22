"""Tests for josuke.audit.

The worktree/forge plumbing and RPC layer are monkeypatched out; the checks
run against fabricated ledger states and fabricated log payloads.
"""

import json

import pytest
from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from josuke import audit
from josuke.ethjsonrpc import RpcError

PROXY = "0x2222222222222222222222222222222222222222"
A1 = to_checksum_address("0x" + "a1" * 20)
B2 = to_checksum_address("0x" + "b2" * 20)
OLD = to_checksum_address("0x" + "0d" * 20)
MIGRATION = to_checksum_address("0x" + "9e" * 20)
ZERO = "0x" + "00" * 20


def _topic_selector(hex4: str) -> str:
    return "0x" + hex4.removeprefix("0x") + "0" * 56


def _topic_address(address_or_zero: str) -> str:
    return "0x" + address_or_zero.removeprefix("0x").lower().rjust(64, "0")


def _delegated_log(selector: str, delegate: str, block: int = 1, tx: str = "0xt1", log_index: int = 0):
    return {
        "address": PROXY.lower(),
        "topics": [audit.SELECTOR_DELEGATED_TOPIC, _topic_selector(selector), _topic_address(delegate)],
        "data": "0x",
        "blockNumber": hex(block),
        "transactionHash": tx,
        "logIndex": hex(log_index),
    }


def _migrate_log(migration: str, calldata: bytes = b"", block: int = 1, tx: str = "0xt0", log_index: int = 0):
    data = "0x" + abi_encode(["bytes"], [calldata]).hex()
    return {
        "address": PROXY.lower(),
        "topics": [audit.DIAMOND_DELEGATE_CALL_TOPIC, _topic_address(migration)],
        "data": data,
        "blockNumber": hex(block),
        "transactionHash": tx,
        "logIndex": hex(log_index),
    }


def _report():
    from josuke.verify import Report

    r = Report()
    r.proxy = PROXY
    return r


def _state(facets, commit="c0"):
    return {"gitCommit": commit, "facets": facets}


# -- find_deploy_block ------------------------------------------------------


def test_find_deploy_block_bisects_to_first_code(monkeypatch):
    # Code appears at block 5 onward.
    monkeypatch.setattr(audit, "eth_get_code", lambda addr, block: "0x60" if int(block, 16) >= 5 else "0x")
    assert audit.find_deploy_block(PROXY, 10) == 5


def test_find_deploy_block_returns_none_when_no_code(monkeypatch):
    monkeypatch.setattr(audit, "eth_get_code", lambda addr, block: "0x")
    assert audit.find_deploy_block(PROXY, 10) is None


def test_find_deploy_block_wraps_rpc_error(monkeypatch):
    def boom(addr, block):
        raise RpcError("eth_getCode: HTTP 500")

    monkeypatch.setattr(audit, "eth_get_code", boom)
    with pytest.raises(Exception, match="historical state"):
        audit.find_deploy_block(PROXY, 10)


# -- get_logs ----------------------------------------------------------------


def test_get_logs_single_span(monkeypatch):
    seen = []

    def fake_get_logs(address, topics, lo, hi):
        seen.append((lo, hi))
        return [_delegated_log("0x11111111", A1, block=lo)]

    monkeypatch.setattr(audit, "eth_get_logs", fake_get_logs)
    logs = audit.get_logs(PROXY, 0, 100)
    assert seen == [(0, 100)]
    assert len(logs) == 1


def test_get_logs_halves_span_on_error_and_recovers(monkeypatch):
    calls = []

    def fake_get_logs(address, topics, lo, hi):
        calls.append((lo, hi))
        if hi - lo + 1 > 25:
            raise RpcError("too many results")
        return [_delegated_log("0x11111111", A1, block=lo)]

    monkeypatch.setattr(audit, "eth_get_logs", fake_get_logs)
    logs = audit.get_logs(PROXY, 0, 99)
    # 100 halves to 50, halves to 25 (both from lo=0, retried in place); once a
    # span succeeds it is reused, unshrunk, for the rest of the scan.
    assert calls == [(0, 99), (0, 49), (0, 24), (25, 49), (50, 74), (75, 99)]
    assert len(logs) == 4


def test_get_logs_propagates_single_block_failure(monkeypatch):
    def boom(address, topics, lo, hi):
        raise RpcError("node error")

    monkeypatch.setattr(audit, "eth_get_logs", boom)
    with pytest.raises(RpcError):
        audit.get_logs(PROXY, 5, 5)


def test_get_logs_sorts_by_block_then_index(monkeypatch):
    unordered = [
        _delegated_log("0x22222222", A1, block=2, log_index=0),
        _delegated_log("0x11111111", A1, block=1, log_index=1),
        _delegated_log("0x33333333", A1, block=1, log_index=0),
    ]
    monkeypatch.setattr(audit, "eth_get_logs", lambda *a: unordered)
    logs = audit.get_logs(PROXY, 0, 10)
    assert [log["topics"][1][:10] for log in logs] == ["0x33333333", "0x11111111", "0x22222222"]


# -- parse_logs ---------------------------------------------------------------


def test_parse_logs_decodes_delegation_and_migration_call():
    logs = [
        _delegated_log("0x11111111", A1, block=1, tx="0xaa"),
        _migrate_log(MIGRATION, b"\x01\x02", block=2, tx="0xbb"),
    ]
    report = _report()
    delegations, calls = audit.parse_logs(logs, report)

    assert report.failures == []
    assert delegations == [audit.Delegation(1, "0xaa", "0x11111111", A1)]
    assert calls == [audit.MigrationCall(2, "0xbb", MIGRATION, b"\x01\x02")]


def test_parse_logs_skips_removed_logs():
    log = _delegated_log("0x11111111", A1)
    log["removed"] = True
    delegations, calls = audit.parse_logs([log], _report())
    assert delegations == []
    assert calls == []


def test_parse_logs_flags_malformed_topics():
    log = _delegated_log("0x11111111", A1)
    log["topics"] = log["topics"][:2]  # missing the delegate topic
    report = _report()
    delegations, calls = audit.parse_logs([log], report)
    assert delegations == []
    assert any("malformed log" in f for f in report.failures)


# -- recorded_delegates -------------------------------------------------------


def test_recorded_delegates_covers_current_proposed_and_history():
    history = {
        "current": {"facets": {"a.sol:A": {"address": A1}}},
        "proposed": {
            "facets": {"b.sol:B": {"address": B2}},
            "selectors": {"address": OLD},
        },
        "history": {to_checksum_address("0x" + "cc" * 20): {"source": "old.sol:Old"}},
    }
    recorded = audit.recorded_delegates(history)
    assert recorded[A1] == "current a.sol:A"
    assert recorded[B2] == "proposed b.sol:B"
    assert recorded[OLD] == "proposed selectors()"
    assert recorded[to_checksum_address("0x" + "cc" * 20)] == "history old.sol:Old"


# -- verify_history_entry ------------------------------------------------------


def test_verify_history_entry_delegates_to_verify_facets(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        audit, "verify_facets",
        lambda state, label, tree, report, cache: seen.update(state=state, label=label),
    )
    entry = {"source": "a.sol:A", "gitCommit": "c0", "codehash": "0x1", "initcodeHash": "0x2"}
    audit.verify_history_entry(A1, entry, ".", _report(), {})

    assert seen["label"] == f"history {A1}"
    assert seen["state"]["facets"]["a.sol:A"] == {
        "codehash": "0x1", "initcodeHash": "0x2", "address": A1,
    }


def test_verify_history_entry_delegates_to_verify_selectors_for_generated_delegate(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        audit, "verify_selectors", lambda state, label, tree, report: seen.update(state=state)
    )
    entry = {"gitCommit": "c0", "selectorsFor": ["a.sol:A", "b.sol:B"]}
    audit.verify_history_entry(OLD, entry, ".", _report(), {})

    assert seen["state"]["selectors"] == {"address": OLD}
    assert set(seen["state"]["facets"]) == {"a.sol:A", "b.sol:B"}


# -- audit_proxy ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_worktrees(monkeypatch):
    """audit_proxy never touches SourceTrees directly; give a trivial `.get`."""

    class Trees:
        def get(self, commit):
            return "."

    return Trees()


def test_audit_proxy_passes_when_delegate_is_current_and_archived(monkeypatch, _no_worktrees):
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])
    monkeypatch.setattr(audit, "verify_facets", lambda *a, **k: None)
    monkeypatch.setattr(audit, "verify_selectors", lambda *a: None)

    history = {
        "current": {"facets": {"a.sol:A": {"address": A1, "codehash": "0x1", "initcodeHash": "0x2"}}},
        "history": {A1: {"source": "a.sol:A", "gitCommit": "c0", "codehash": "0x1", "initcodeHash": "0x2"}},
    }
    report = _report()
    unarchived = audit.audit_proxy(PROXY, history, "314", 10, 0, _no_worktrees, report, {})

    assert report.failures == []
    assert unarchived == 0


def test_audit_proxy_flags_unrecorded_delegate(monkeypatch, _no_worktrees):
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])

    history = {"current": {"facets": {}}}
    report = _report()
    audit.audit_proxy(PROXY, history, "314", 10, 0, _no_worktrees, report, {})

    assert any("not recorded" in f for f in report.failures)


def test_audit_proxy_counts_recorded_but_unarchived_delegate(monkeypatch, _no_worktrees):
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])

    history = {"current": {"facets": {"a.sol:A": {"address": A1}}}}
    report = _report()
    unarchived = audit.audit_proxy(PROXY, history, "314", 10, 0, _no_worktrees, report, {})

    assert report.failures == []  # recorded in current, so not a failure
    assert unarchived == 1


def test_audit_proxy_ignores_removal_delegations(monkeypatch, _no_worktrees):
    monkeypatch.setattr(
        audit, "get_logs",
        lambda *a: [
            _delegated_log("0x11111111", A1, block=1),
            _delegated_log("0x11111111", ZERO, block=2),
        ],
    )
    history = {"current": {"facets": {"a.sol:A": {"address": A1}}}}
    report = _report()
    unarchived = audit.audit_proxy(PROXY, history, "314", 10, 0, _no_worktrees, report, {})

    assert report.failures == []
    assert unarchived == 1  # only the install is tallied, not the removal


def test_audit_proxy_finds_deploy_block_when_not_given(monkeypatch, _no_worktrees):
    monkeypatch.setattr(audit, "find_deploy_block", lambda proxy, latest: 42)
    seen = {}

    def fake_get_logs(p, lo, hi):
        seen["range"] = (lo, hi)
        return []

    monkeypatch.setattr(audit, "get_logs", fake_get_logs)

    history = {"current": {"facets": {}}}
    audit.audit_proxy(PROXY, history, "314", 100, None, _no_worktrees, _report(), {})
    assert seen["range"] == (42, 100)


def test_audit_proxy_fails_when_never_deployed(monkeypatch, _no_worktrees):
    monkeypatch.setattr(audit, "find_deploy_block", lambda proxy, latest: None)
    report = _report()
    audit.audit_proxy(PROXY, {"current": {"facets": {}}}, "314", 100, None, _no_worktrees, report, {})
    assert any("no code at" in f for f in report.failures)


def test_audit_proxy_verifies_each_history_entry(monkeypatch, _no_worktrees):
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])
    calls = []
    monkeypatch.setattr(
        audit, "verify_facets", lambda state, label, tree, report, cache: calls.append(label)
    )

    history = {
        "current": {"facets": {"a.sol:A": {"address": A1}}},
        "history": {A1: {"source": "a.sol:A", "gitCommit": "c0", "codehash": "0x1", "initcodeHash": "0x2"}},
    }
    audit.audit_proxy(PROXY, history, "314", 10, 0, _no_worktrees, _report(), {})
    assert calls == [f"history {A1}"]


# -- run_audit -----------------------------------------------------------------


@pytest.fixture
def stub(monkeypatch):
    class Trees:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, commit):
            return "."

    monkeypatch.setattr(audit, "SourceTrees", lambda root: Trees())
    monkeypatch.setattr(audit, "chain_id", lambda: "314")
    monkeypatch.setattr(audit, "eth_block_number", lambda: 10)
    monkeypatch.setattr(audit, "verify_facets", lambda *a, **k: None)
    monkeypatch.setattr(audit, "verify_selectors", lambda *a: None)
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")


def _ledger(tmp_path, entries):
    p = tmp_path / "josuke.json"
    p.write_text(json.dumps(entries))
    return p


def test_run_audit_requires_rpc_url(monkeypatch, tmp_path):
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="ETH_RPC_URL"):
        audit.run_audit(_ledger(tmp_path, []))


def test_run_audit_reports_nothing_recorded(monkeypatch, tmp_path, stub, capsys):
    monkeypatch.chdir(tmp_path)
    entry = {"address": PROXY, "facetSrc": ["*.sol"], "deployments": {}}
    audit.run_audit(_ledger(tmp_path, [entry]))
    assert "nothing recorded on chain 314" in capsys.readouterr().out


def test_run_audit_fails_on_unrecorded_delegate(monkeypatch, tmp_path, stub):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])
    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {"314": {"current": {"gitCommit": "c0", "facets": {}}}},
    }
    with pytest.raises(Exception, match="audit failure"):
        audit.run_audit(_ledger(tmp_path, [entry]), from_block=0)


def test_run_audit_passes_clean_ledger(monkeypatch, tmp_path, stub, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])
    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {
                "current": {"gitCommit": "c0", "facets": {"a.sol:A": {"address": A1}}},
                "history": {
                    A1: {
                        "source": "a.sol:A", "gitCommit": "c0",
                        "codehash": "0x1", "initcodeHash": "0x2",
                    }
                },
            }
        },
    }
    audit.run_audit(_ledger(tmp_path, [entry]), from_block=0)
    out = capsys.readouterr().out
    assert "every installed delegate is recorded and verified" in out


def test_run_audit_warns_once_for_unarchived_delegates(monkeypatch, tmp_path, stub, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit, "get_logs", lambda *a: [_delegated_log("0x11111111", A1)])
    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {"current": {"gitCommit": "c0", "facets": {"a.sol:A": {"address": A1}}}}
        },
    }
    audit.run_audit(_ledger(tmp_path, [entry]), from_block=0)
    err = capsys.readouterr().err
    assert err.count("`josuke accept` records the delegates it accepts") == 1
