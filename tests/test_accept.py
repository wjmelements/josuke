"""Tests for josuke.accept.

The worktree/forge plumbing is monkeypatched out; the checks run against
fabricated ledger states and a fake ProxyStorage.
"""

import json

import pytest
from eth_utils import to_checksum_address

from josuke import accept, verify

PROXY = "0x2222222222222222222222222222222222222222"
A1 = to_checksum_address("0x" + "a1" * 20)
B2 = to_checksum_address("0x" + "b2" * 20)
OLD = to_checksum_address("0x" + "0d" * 20)
ZERO = "0x" + "00" * 20


def _word(address_or_zero: str) -> str:
    return "0x" + address_or_zero.removeprefix("0x").rjust(64, "0")


class FakeStorage:
    def __init__(self, address, values=None):
        self.address = address
        self.storage_keys = {}
        self.storage_values = dict(values or {})

    def fetch(self, selectors):
        for s in selectors:
            self.storage_keys[s.selector] = "0x" + s.selector[2:].rjust(64, "e")
            self.storage_values.setdefault(s.selector, "0x" + "00" * 32)


def _sel(hex4):
    from josuke.selectors import Selector

    return Selector(hex4, f"fn{hex4}()")


@pytest.fixture(autouse=True)
def _isolate_maps():
    from josuke import delegate as _d
    from josuke import selectors as _s

    sm, dm = dict(_s.selector_map), dict(_d.source_map)
    _s.selector_map.clear()
    _d.source_map.clear()
    yield
    _s.selector_map.clear()
    _s.selector_map.update(sm)
    _d.source_map.clear()
    _d.source_map.update(dm)


def _report():
    r = verify.Report()
    r.proxy = PROXY
    return r


def _state(facets, commit="c0"):
    return {"gitCommit": commit, "facets": facets}


def _owns(monkeypatch, selectors_by_source):
    monkeypatch.setattr(
        verify,
        "facet_selectors",
        lambda facet, tree: selectors_by_source.get(facet.source_id, []),
    )


# -- verify_migrated -------------------------------------------------------


def test_verify_migrated_passes_when_routes_match(monkeypatch):
    _owns(monkeypatch, {"a.sol:A": [_sel("0x11111111")], "b.sol:B": [_sel("0x22222222")]})
    proposed = _state({"a.sol:A": {"address": A1}, "b.sol:B": {"address": B2}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1), "0x22222222": _word(B2)})

    report = _report()
    accept.verify_migrated(None, proposed, storage, None, ".", report)
    assert report.failures == []


def test_verify_migrated_flags_selector_not_routed(monkeypatch):
    _owns(monkeypatch, {"a.sol:A": [_sel("0x11111111")]})
    proposed = _state({"a.sol:A": {"address": A1}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(ZERO)})  # migration never ran

    report = _report()
    accept.verify_migrated(None, proposed, storage, None, ".", report)
    assert len(report.failures) == 1
    assert "migration not run" in report.failures[0]


def test_verify_migrated_flags_unremoved_selector(monkeypatch):
    _owns(
        monkeypatch,
        {"keep.sol:Keep": [_sel("0x11111111")], "old.sol:Old": [_sel("0x99999999")]},
    )
    current = _state({"keep.sol:Keep": {"address": A1}, "old.sol:Old": {"address": OLD}})
    proposed = _state({"keep.sol:Keep": {"address": A1}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1), "0x99999999": _word(OLD)})

    report = _report()
    accept.verify_migrated(current, proposed, storage, ".", ".", report)
    assert any("0x99999999" in f and "expected removal" in f for f in report.failures)


def test_verify_migrated_accepts_already_zeroed_removal(monkeypatch):
    _owns(
        monkeypatch,
        {"keep.sol:Keep": [_sel("0x11111111")], "old.sol:Old": [_sel("0x99999999")]},
    )
    current = _state({"keep.sol:Keep": {"address": A1}, "old.sol:Old": {"address": OLD}})
    proposed = _state({"keep.sol:Keep": {"address": A1}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1), "0x99999999": _word(ZERO)})

    report = _report()
    accept.verify_migrated(current, proposed, storage, ".", ".", report)
    assert report.failures == []


def test_verify_migrated_flags_missing_address(monkeypatch):
    _owns(monkeypatch, {"a.sol:A": [_sel("0x11111111")]})
    proposed = _state({"a.sol:A": {}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1)})

    report = _report()
    accept.verify_migrated(None, proposed, storage, None, ".", report)
    assert any("no recorded address" in f for f in report.failures)


# -- accepted_state -------------------------------------------------------


def test_accepted_state_is_a_wholesale_swap():
    current = _state({"gone.sol:Gone": {"address": OLD}}, commit="old")
    proposed = {
        "gitCommit": "new",
        "facets": {"a.sol:A": {"address": A1}},
        "migration": {"address": OLD},
    }
    assert accept.accepted_state(current, proposed) == {
        "gitCommit": "new",
        "facets": {"a.sol:A": {"address": A1}},
        "migration": {"address": OLD},
    }


def test_accepted_state_without_migration():
    got = accept.accepted_state({}, {"gitCommit": "n", "facets": {"a.sol:A": {}}})
    assert "migration" not in got


def test_accepted_state_carries_selectors():
    proposed = {
        "gitCommit": "new",
        "facets": {"a.sol:A": {"address": A1}},
        "selectors": {"address": B2},
    }
    assert accept.accepted_state({}, proposed)["selectors"] == {"address": B2}


def test_verify_migrated_checks_generated_selectors_routing(monkeypatch):
    _owns(monkeypatch, {"a.sol:A": [_sel("0x11111111")]})
    proposed = _state({"a.sol:A": {"address": A1}}) | {"selectors": {"address": B2}}
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1), "0x6e25b978": _word(OLD)})

    report = _report()
    accept.verify_migrated(None, proposed, storage, None, ".", report)
    assert any("0x6e25b978" in f and "migration not run" in f for f in report.failures)


def test_verify_migrated_passes_with_generated_selectors_routed(monkeypatch):
    _owns(monkeypatch, {"a.sol:A": [_sel("0x11111111")]})
    proposed = _state({"a.sol:A": {"address": A1}}) | {"selectors": {"address": B2}}
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1), "0x6e25b978": _word(B2)})

    report = _report()
    accept.verify_migrated(None, proposed, storage, None, ".", report)
    assert report.failures == []


# -- history_entries -------------------------------------------------------


def test_history_entries_indexes_facets_by_address():
    state = _state(
        {
            "a.sol:A": {
                "address": A1, "codehash": "0x1", "initcodeHash": "0x2",
                "constructorArgs": {"x": 1}, "from": B2,
            }
        },
        commit="c1",
    )
    assert accept.history_entries(state) == {
        A1: {
            "source": "a.sol:A", "gitCommit": "c1",
            "codehash": "0x1", "initcodeHash": "0x2",
            "constructorArgs": {"x": 1}, "from": B2,
        }
    }


def test_history_entries_skips_facets_with_no_address():
    state = _state({"a.sol:A": {"codehash": "0x1", "initcodeHash": "0x2"}})
    assert accept.history_entries(state) == {}


def test_history_entries_records_generated_selectors_by_facet_set():
    state = _state({"a.sol:A": {"address": A1}, "b.sol:B": {"address": B2}}, commit="c1")
    state["selectors"] = {"address": OLD}
    assert accept.history_entries(state)[OLD] == {
        "gitCommit": "c1", "selectorsFor": ["a.sol:A", "b.sol:B"],
    }


def test_history_entries_omits_selectors_when_absent():
    state = _state({"a.sol:A": {"address": A1}})
    assert OLD not in accept.history_entries(state)


# -- run_accept ---------------------------------------------------------


@pytest.fixture
def stub(monkeypatch):
    """Neutralise worktrees/chain; return a mutable selector -> storage-word map."""
    routes: dict[str, str] = {}

    class Trees:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, commit):
            return "."

    monkeypatch.setattr(accept, "SourceTrees", lambda root: Trees())
    monkeypatch.setattr(accept, "chain_id", lambda: "314")
    monkeypatch.setattr(accept, "ProxyStorage", lambda addr: FakeStorage(addr, routes))
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    return routes


def _ledger(tmp_path, entries):
    p = tmp_path / "josuke.json"
    p.write_text(json.dumps(entries))
    return p


def test_run_accept_requires_rpc_url(monkeypatch, tmp_path):
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="ETH_RPC_URL"):
        accept.run_accept(_ledger(tmp_path, []))


def test_run_accept_merges_proposed_into_current(monkeypatch, tmp_path, stub):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    stub["0x11111111"] = _word(A1)

    proposed = {
        "gitCommit": "c1",
        "facets": {"a.sol:A": {"address": A1, "initcodeHash": "0x3", "codehash": "0x4"}},
        "migration": {"address": OLD},
    }
    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {
                "current": {
                    "gitCommit": "c0",
                    "facets": {"a.sol:A": {"address": OLD, "initcodeHash": "0x1", "codehash": "0x2"}},
                },
                "proposed": proposed,
            }
        },
    }
    path = _ledger(tmp_path, [entry])

    accept.run_accept(path)

    history = json.loads(path.read_text())[0]["deployments"]["314"]
    assert "proposed" not in history
    assert history["current"] == proposed


def test_run_accept_first_deployment_has_no_current(monkeypatch, tmp_path, stub):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    stub["0x11111111"] = _word(A1)

    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {
                "proposed": {
                    "gitCommit": "c1",
                    "facets": {"a.sol:A": {"address": A1}},
                    "migration": {"address": OLD},
                }
            }
        },
    }
    path = _ledger(tmp_path, [entry])

    accept.run_accept(path)

    history = json.loads(path.read_text())[0]["deployments"]["314"]
    assert "proposed" not in history
    assert history["current"]["gitCommit"] == "c1"


def test_run_accept_leaves_ledger_untouched_on_failure(monkeypatch, tmp_path, stub):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    # no route seeded: 0x11111111 reads zero, so the migration did not run

    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {
                "proposed": {
                    "gitCommit": "c1",
                    "facets": {"a.sol:A": {"address": A1}},
                    "migration": {"address": OLD},
                }
            }
        },
    }
    path = _ledger(tmp_path, [entry])
    before = path.read_text()

    with pytest.raises(Exception, match="acceptance check"):
        accept.run_accept(path)
    assert path.read_text() == before


def test_run_accept_reports_when_nothing_proposed(monkeypatch, tmp_path, stub, capsys):
    monkeypatch.chdir(tmp_path)
    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {"current": {"gitCommit": "c0", "facets": {"a.sol:A": {"address": A1}}}}
        },
    }
    path = _ledger(tmp_path, [entry])

    accept.run_accept(path)
    assert "no proposed deployment on chain 314" in capsys.readouterr().out
