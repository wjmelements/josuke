"""Tests for josuke.verify.

The worktree/forge plumbing is monkeypatched out; the checks themselves run
against fabricated ledger states, a fake ProxyStorage, and a fake eth_getCode.
"""

import json
import shutil
import subprocess

import pytest
from eth_utils import to_checksum_address

from josuke import deploy, verify
from josuke.migration import SET_DELEGATE_SIZE

PROXY = "0x2222222222222222222222222222222222222222"
A1 = to_checksum_address("0x" + "a1" * 20)
B2 = to_checksum_address("0x" + "b2" * 20)
OLD = to_checksum_address("0x" + "0d" * 20)


def _word(address_or_zero: str) -> str:
    return "0x" + address_or_zero.removeprefix("0x").rjust(64, "0")


class FakeStorage:
    def __init__(self, address, values=None):
        self.address = address
        self.storage_keys = {}
        self.storage_values = values or {}

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


# -- pure helpers ---------------------------------------------------------------


def test_slot_address_empty():
    assert verify._slot_address(None) is None
    assert verify._slot_address("0x" + "00" * 32) is None


def test_slot_address_extracts_low_20_bytes():
    assert verify._slot_address(_word(A1)) == A1


# -- verify_facets ------------------------------------------------------------


def _state(facets, commit="c0"):
    return {"gitCommit": commit, "facets": facets}


def test_verify_facets_flags_initcode_and_codehash(monkeypatch):
    monkeypatch.setattr(verify, "facet_initcode", lambda f, root, args, prompt: ("dead", None))
    monkeypatch.setattr(verify, "keccak_hex", lambda h: "0x" + "11" * 32)
    monkeypatch.setattr(verify, "_onchain_codehash", lambda addr: "0x" + "99" * 32)

    state = _state({
        "a.sol:A": {"address": A1, "initcodeHash": "0x" + "11" * 32, "codehash": "0x" + "22" * 32},
    })
    report = _report()
    verify.verify_facets(state, "current", ".", report)

    assert len(report.failures) == 1
    assert "codehash" in report.failures[0]


def test_verify_facets_flags_missing_address(monkeypatch):
    monkeypatch.setattr(verify, "facet_initcode", lambda f, root, args, prompt: ("dead", None))
    monkeypatch.setattr(verify, "keccak_hex", lambda h: "0xok")

    state = _state({"a.sol:A": {"initcodeHash": "0xok", "codehash": "0xwhatever"}})
    report = _report()
    verify.verify_facets(state, "proposed", ".", report)

    assert report.failures == [f"{PROXY} proposed a.sol:A: no recorded address; cannot check deployed code"]


# -- verify_proposed_set ----------------------------------------------------


def test_verify_proposed_set_reports_missing_and_extra(monkeypatch):
    monkeypatch.setattr(
        verify, "resolve_facets", lambda src, tree: [deploy.facet_from_source_id("a.sol:A"),
                                                     deploy.facet_from_source_id("b.sol:B")]
    )
    proposed = _state({"a.sol:A": {}, "c.sol:C": {}})
    report = _report()
    verify.verify_proposed_set(["*.sol"], proposed, ".", report)

    assert any("b.sol:B resolves from facetSrc" in f for f in report.failures)
    assert any("c.sol:C" in f and "not matched by facetSrc" in f for f in report.failures)


# -- verify_dispatch --------------------------------------------------------


def test_verify_dispatch_flags_wrong_route(monkeypatch):
    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    current = _state({"a.sol:A": {"address": A1}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(B2)})  # proxy points at B2, not A1
    report = _report()
    verify.verify_dispatch(current, storage, ".", report)

    assert len(report.failures) == 1
    assert "routes to" in report.failures[0] and A1 in report.failures[0]


def test_verify_dispatch_passes_when_route_matches(monkeypatch):
    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    current = _state({"a.sol:A": {"address": A1}})
    storage = FakeStorage(PROXY, {"0x11111111": _word(A1)})
    report = _report()
    verify.verify_dispatch(current, storage, ".", report)

    assert report.failures == []


# -- verify_selectors -----------------------------------------------------


def _generated_runtime(selector_lists):
    from josuke.erc8167 import generated_selectors, selectors_method

    return selectors_method(generated_selectors(selector_lists))


def test_verify_selectors_passes_when_code_matches(monkeypatch):
    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel("0x11111111")])
    runtime = _generated_runtime([[_sel("0x11111111")]])
    monkeypatch.setattr(verify, "eth_get_code", lambda address: "0x" + runtime.hex())

    report = _report()
    verify.verify_selectors(_state({"a.sol:A": {"address": A1}}) | {"selectors": {"address": B2}}, "proposed", ".", report)
    assert report.failures == []


def test_verify_selectors_flags_stale_code(monkeypatch):
    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel("0x11111111")])
    monkeypatch.setattr(verify, "eth_get_code", lambda address: "0x" + "00" * 8)

    report = _report()
    verify.verify_selectors(_state({"a.sol:A": {"address": A1}}) | {"selectors": {"address": B2}}, "proposed", ".", report)
    assert any("not the generated selectors()" in f for f in report.failures)


def test_verify_selectors_flags_record_when_a_facet_implements_it(monkeypatch):
    from josuke.erc8167 import SELECTORS_SELECTOR

    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel(SELECTORS_SELECTOR)])
    report = _report()
    verify.verify_selectors(_state({"a.sol:A": {"address": A1}}) | {"selectors": {"address": B2}}, "proposed", ".", report)
    assert any("should not be recorded" in f for f in report.failures)


def test_verify_selectors_noop_without_record():
    report = _report()
    verify.verify_selectors(_state({"a.sol:A": {"address": A1}}), "current", ".", report)
    assert report.failures == []


def test_selector_owners_includes_generated_selectors(monkeypatch):
    from josuke.erc8167 import SELECTORS_SELECTOR

    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    owners, _ = verify._selector_owners(
        _state({"a.sol:A": {"address": A1}}) | {"selectors": {"address": B2}}, "."
    )
    assert owners[SELECTORS_SELECTOR] == ("selectors()", B2)


# -- verify_migration -----------------------------------------------------


def _migration_setup(monkeypatch, selectors_by_source):
    monkeypatch.setattr(
        deploy, "facet_selectors", lambda facet, root: selectors_by_source[facet.source_id]
    )


def _fetched_storage(all_selectors, values=None):
    storage = FakeStorage(PROXY, values)
    storage.fetch([_sel(s) for s in all_selectors])
    return storage


def test_verify_migration_passes_for_recomputed_bytecode(monkeypatch):
    _migration_setup(monkeypatch, {"a.sol:A": [_sel("0x11111111")]})
    proposed = _state({"a.sol:A": {"address": A1}})
    storage = _fetched_storage(["0x11111111"])
    expected = deploy.build_migration(
        PROXY, [deploy.facet_from_source_id("a.sol:A")], proposed["facets"], {}, ".", storage=storage
    )
    monkeypatch.setattr(verify, "eth_get_code", lambda address: "0x" + expected.encode().hex())

    report = _report()
    verify.verify_migration(PROXY, {**proposed, "migration": {"address": OLD}}, {}, storage, ".", report)
    assert report.failures == []


def test_verify_migration_flags_missing_install(monkeypatch):
    _migration_setup(monkeypatch, {"a.sol:A": [_sel("0x11111111")], "b.sol:B": [_sel("0x22222222")]})
    proposed = _state({"a.sol:A": {"address": A1}, "b.sol:B": {"address": B2}})
    storage = _fetched_storage(["0x11111111", "0x22222222"])
    facets = [deploy.facet_from_source_id("a.sol:A"), deploy.facet_from_source_id("b.sol:B")]
    expected = deploy.build_migration(PROXY, facets, proposed["facets"], {}, ".", storage=storage)
    # on-chain migration only carries the first fragment
    onchain = expected.encode()[:SET_DELEGATE_SIZE]
    monkeypatch.setattr(verify, "eth_get_code", lambda address: "0x" + onchain.hex())

    report = _report()
    verify.verify_migration(PROXY, {**proposed, "migration": {"address": OLD}}, {}, storage, ".", report)

    assert any("does not install" in f and "0x22222222" in f for f in report.failures)


def test_verify_migration_flags_unzeroed_removal(monkeypatch):
    _migration_setup(monkeypatch, {
        "keep.sol:Keep": [_sel("0x11111111")],
        "old.sol:Old": [_sel("0x99999999")],
    })
    proposed = _state({"keep.sol:Keep": {"address": A1}})
    current = _state({"keep.sol:Keep": {}, "old.sol:Old": {}})
    # 0x99999999 currently routes somewhere non-zero, so it must be zeroed
    storage = _fetched_storage(["0x11111111", "0x99999999"], {"0x99999999": _word(OLD)})
    facets = [deploy.facet_from_source_id("keep.sol:Keep")]
    expected = deploy.build_migration(PROXY, facets, proposed["facets"], current, ".", storage=storage)
    # drop the zeroing fragment (the one whose delegate is the zero address)
    from josuke.migration import Migration

    kept = [sd for sd in expected.setdelegates if sd.delegate.address != verify.ZERO_ADDRESS]
    monkeypatch.setattr(verify, "eth_get_code", lambda address: "0x" + Migration(kept).encode().hex())

    report = _report()
    verify.verify_migration(
        PROXY, {**proposed, "migration": {"address": OLD}}, current, storage, ".", report
    )

    assert any("does not zero selector 0x99999999" in f for f in report.failures)


# -- SourceTrees (real git, stubbed forge) --------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_source_trees_checks_out_each_commit_and_cleans_up(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.sol").write_text("contract A { function a() external {} }\n")
    git("add", "-A")
    git("commit", "-qm", "one")
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "src" / "A.sol").write_text("contract A { function a() external {} function b() external {} }\n")
    git("add", "-A")
    git("commit", "-qm", "two")

    builds = []
    real_run = verify.run
    monkeypatch.setattr(
        verify,
        "run",
        lambda cmd, root=None, stdin=None: builds.append(str(root)) or ""
        if cmd[:2] == ["forge", "build"]
        else real_run(cmd, root, stdin),
    )

    with verify.SourceTrees(tmp_path) as trees:
        tree = trees.get(first)
        assert trees.get(first) is tree  # cached
        assert (tree / "src" / "A.sol").read_text().count("function") == 1
        assert builds == [str(tree)]
        parent = tree.parent

    assert not parent.exists()
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout
    assert str(tmp_path / "x") not in listed  # only the main worktree remains


# -- run_verify -----------------------------------------------------------


def _ledger(tmp_path, entries):
    p = tmp_path / "josuke.json"
    p.write_text(json.dumps(entries))
    return p


@pytest.fixture
def stub_trees(monkeypatch):
    class Trees:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, commit):
            return "."

    monkeypatch.setattr(verify, "SourceTrees", lambda root: Trees())
    monkeypatch.setattr(verify, "chain_id", lambda: "314")
    monkeypatch.setattr(verify, "ProxyStorage", FakeStorage)


def test_run_verify_requires_rpc_url(monkeypatch, tmp_path):
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="ETH_RPC_URL"):
        verify.run_verify(_ledger(tmp_path, []))


def test_run_verify_skips_chain_without_deployment(monkeypatch, tmp_path, stub_trees, capsys):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    monkeypatch.chdir(tmp_path)
    path = _ledger(tmp_path, [{"address": PROXY, "facetSrc": ["*.sol"], "deployments": {}}])

    verify.run_verify(path)
    assert "nothing recorded on chain 314" in capsys.readouterr().out


def test_run_verify_reports_failures(monkeypatch, tmp_path, stub_trees):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verify, "facet_selectors", lambda facet, tree: [_sel("0x11111111")])
    monkeypatch.setattr(verify, "facet_initcode", lambda f, root, args, prompt: ("dead", None))
    monkeypatch.setattr(verify, "keccak_hex", lambda h: "0xbad")
    monkeypatch.setattr(verify, "_onchain_codehash", lambda addr: "0xbad")

    entry = {
        "address": PROXY,
        "facetSrc": ["*.sol"],
        "deployments": {
            "314": {
                "current": {
                    "gitCommit": "c0",
                    "facets": {"a.sol:A": {"address": A1, "initcodeHash": "0xgood", "codehash": "0xbad"}},
                }
            }
        },
    }
    with pytest.raises(Exception, match="initcodeHash"):
        verify.run_verify(_ledger(tmp_path, [entry]))
