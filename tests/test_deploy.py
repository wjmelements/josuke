"""Tests for josuke.deploy.

The pure helpers are tested directly. `resolve_facets` runs against the committed
Foundry fixture (skipped without `forge`). `run_deploy`'s decision logic is tested
with the chain-touching helpers monkeypatched out.
"""

import json
import pathlib
import shutil

import pytest
from eth_utils import keccak, to_checksum_address

from josuke import deploy
from josuke.deploy import Facet, coerce_arg, keccak_hex, resolve_facets

A1 = to_checksum_address("0x" + "a1" * 20)  # deploy_initcode always returns checksummed
B2 = to_checksum_address("0x" + "b2" * 20)

FIXTURE_ROOT = pathlib.Path(__file__).parent / "fixtures" / "forge-project"


# -- pure helpers -------------------------------------------------------------


def test_keccak_hex_matches_keccak():
    assert keccak_hex("0x1234") == "0x" + keccak(b"\x12\x34").hex()
    assert keccak_hex("1234") == keccak_hex("0x1234")


@pytest.mark.parametrize(
    "abi_type, value, expected",
    [
        ("uint256", 123, 123),
        ("address", "0x00000000000000000000000000000000000000ff", "0x" + "00" * 19 + "ff"),
        ("bytes32", "0x" + "ab" * 32, b"\xab" * 32),
        ("bytes4", "deadbeef", b"\xde\xad\xbe\xef"),
        ("bool", True, True),
        ("string", "NWO", "NWO"),
        ("address[]", ["0x" + "11" * 20], ["0x" + "11" * 20]),
        ("bytes32[]", ["0x" + "00" * 32], [b"\x00" * 32]),
    ],
)
def test_coerce_arg(abi_type, value, expected):
    assert coerce_arg(abi_type, value) == expected


# -- resolve_facets ---------------------------------------------------------

requires_forge = pytest.mark.skipif(
    shutil.which("forge") is None, reason="requires the `forge` binary"
)


@requires_forge
def test_resolve_facets_expands_glob_to_deployable_contracts():
    facets = resolve_facets(["src/*.sol"], FIXTURE_ROOT)
    assert {f.source_id for f in facets} == {
        "src/FromDeployer.sol:FromDeployer",
        "src/NoArgs.sol:NoArgs",
        "src/WithArgs.sol:WithArgs",
    }
    assert all(f.kind == "sol" for f in facets)


@requires_forge
def test_resolve_facets_passes_through_explicit_and_evm_and_dedupes():
    facets = resolve_facets(
        ["src/NoArgs.sol:NoArgs", "lib/x/impl.evm", "src/*.sol"], FIXTURE_ROOT
    )
    ids = [f.source_id for f in facets]
    assert ids[0] == "src/NoArgs.sol:NoArgs"
    assert "lib/x/impl.evm" in ids
    assert ids.count("src/NoArgs.sol:NoArgs") == 1  # not re-added by the glob
    evm = next(f for f in facets if f.source_id == "lib/x/impl.evm")
    assert evm.kind == "evm"


# -- facet_initcode -------------------------------------------------------


def test_facet_initcode_uses_evm_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "evm_artifact",
        lambda source, root: {"initcode": "deadbeef", "abi": []},
    )
    initcode, args = deploy.facet_initcode(Facet("evm", "impl.evm", None, "impl.evm"), tmp_path, None)
    assert initcode == "deadbeef"
    assert args is None


# -- run_deploy decision logic -----------------------------------------------


@pytest.fixture
def stub_chain(monkeypatch, tmp_path):
    """Neutralise every chain / toolchain call in deploy; return a mutable log."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc")
    monkeypatch.setattr(deploy, "chain_id", lambda: "314")
    monkeypatch.setattr(deploy, "git_commit", lambda root: "f" * 40)
    monkeypatch.setattr(deploy, "run", lambda *a, **k: "")  # forge build
    monkeypatch.setattr(deploy, "code_hash", lambda addr: "0x" + "cc" * 32)
    # A truthy stand-in for "migration ready": most of these tests care about
    # facet/selectors handling, not migration content, but `proposed` is now
    # only recorded when a migration is needed, so it must not be None here.
    monkeypatch.setattr(deploy, "build_migration", lambda *a, **k: object())
    monkeypatch.setattr(deploy, "deploy_migration", lambda *a, **k: {"address": "0x" + "dd" * 20})
    monkeypatch.setattr(deploy, "selectors_runtime", lambda *a, **k: None)

    log = {"deployed": []}
    counter = [0]

    def fake_initcode(facet, root, recorded_args):
        # source_id doubles as the "bytecode": lets tests force a change.
        return facet.source_id.encode().hex(), (recorded_args or None)

    def fake_deploy(initcode_hex, root):
        counter[0] += 1
        addr = "0x" + f"{counter[0]:040x}"
        log["deployed"].append(initcode_hex)
        return addr, DEPLOYER

    monkeypatch.setattr(deploy, "facet_initcode", fake_initcode)
    monkeypatch.setattr(deploy, "deploy_initcode", fake_deploy)
    # differential replay needs a live `evm`/RPC; default to "sender-independent"
    monkeypatch.setattr(deploy, "deployer_derived", lambda initcode, sender: False)
    return log


def _write(tmp_path, ledger):
    p = tmp_path / "josuke.json"
    p.write_text(json.dumps(ledger))
    return p


def _resolve_to(monkeypatch, source_ids):
    monkeypatch.setattr(
        deploy,
        "resolve_facets",
        lambda facet_src, root: [Facet("evm", s, None, s) for s in source_ids],
    )


PROXY = "0x2222222222222222222222222222222222222222"
DEPLOYER = to_checksum_address("0x" + "de" * 20)  # cast-receipt `from`, per fake_deploy


def test_run_deploy_first_time_deploys_all_into_proposed(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm", "b.evm"])
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    entry = json.loads(path.read_text())[0]
    assert "current" not in entry["deployments"]["314"]
    proposed = entry["deployments"]["314"]["proposed"]
    assert proposed["gitCommit"] == "f" * 40
    assert set(proposed["facets"]) == {"a.evm", "b.evm"}
    assert len(stub_chain["deployed"]) == 2


def test_run_deploy_carries_unchanged_and_redeploys_changed(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm", "b.evm"])
    unchanged = {
        "address": "0x" + "ab" * 20,
        "codehash": "0x" + "11" * 32,
        "initcodeHash": keccak_hex("a.evm".encode().hex()),
    }
    stale = {
        "address": "0x" + "cd" * 20,
        "codehash": "0x" + "22" * 32,
        "initcodeHash": "0x" + "00" * 32,  # will not match -> redeploy
    }
    path = _write(
        tmp_path,
        [
            {
                "address": PROXY,
                "facetSrc": ["*.evm"],
                "deployments": {
                    "314": {
                        "current": {
                            "gitCommit": "e" * 40,
                            "facets": {"a.evm": unchanged, "b.evm": stale},
                        }
                    }
                },
            }
        ],
    )

    deploy.run_deploy(path)

    proposed = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert proposed["facets"]["a.evm"] == unchanged  # carried over verbatim
    assert proposed["facets"]["b.evm"]["address"] == "0x" + f"{1:040x}"
    assert stub_chain["deployed"] == ["b.evm".encode().hex()]  # only the stale one


def test_run_deploy_all_redeploys_unchanged_facets(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm", "b.evm"])
    unchanged = {
        "address": "0x" + "ab" * 20,
        "codehash": "0x" + "11" * 32,
        "initcodeHash": keccak_hex("a.evm".encode().hex()),
    }
    path = _write(
        tmp_path,
        [
            {
                "address": PROXY,
                "facetSrc": ["*.evm"],
                "deployments": {
                    "314": {
                        "current": {
                            "gitCommit": "e" * 40,
                            "facets": {"a.evm": unchanged, "b.evm": unchanged},
                        }
                    }
                },
            }
        ],
    )

    deploy.run_deploy(path, redeploy_all=True)

    proposed = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert proposed["facets"]["a.evm"]["address"] == "0x" + f"{1:040x}"
    assert proposed["facets"]["b.evm"]["address"] == "0x" + f"{2:040x}"
    assert sorted(stub_chain["deployed"]) == sorted(
        [s.encode().hex() for s in ("a.evm", "b.evm")]
    )


def test_run_deploy_is_idempotent_before_promotion(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm", "b.evm"])
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)
    first = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert len(stub_chain["deployed"]) == 2

    deploy.run_deploy(path)  # nothing changed, nothing promoted
    second = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert second["facets"] == first["facets"]
    assert len(stub_chain["deployed"]) == 2  # no new transactions


def test_run_deploy_shares_one_deployment_across_proxies(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["shared.evm"])
    path = _write(
        tmp_path,
        [
            {"address": PROXY, "facetSrc": ["*.evm"]},
            {"address": "0x" + "33" * 20, "facetSrc": ["*.evm"]},
        ],
    )

    deploy.run_deploy(path)

    entries = json.loads(path.read_text())
    a = entries[0]["deployments"]["314"]["proposed"]["facets"]["shared.evm"]
    b = entries[1]["deployments"]["314"]["proposed"]["facets"]["shared.evm"]
    assert a["address"] == b["address"]
    assert len(stub_chain["deployed"]) == 1  # deployed once, reused for the second proxy


def test_run_deploy_records_constructor_args(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])
    path = _write(
        tmp_path,
        [
            {
                "address": PROXY,
                "facetSrc": ["*.evm"],
                "deployments": {
                    "314": {
                        "current": {
                            "gitCommit": "e" * 40,
                            "facets": {
                                "a.evm": {
                                    "address": "0x" + "ab" * 20,
                                    "codehash": "0x" + "11" * 32,
                                    "initcodeHash": "0x" + "00" * 32,
                                    "constructorArgs": {"owner": "0x" + "12" * 20},
                                }
                            },
                        }
                    }
                },
            }
        ],
    )

    deploy.run_deploy(path)

    facet = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]["facets"]["a.evm"]
    assert facet["constructorArgs"] == {"owner": "0x" + "12" * 20}


def test_run_deploy_carries_from_forward_on_redeploy(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])
    deployer = "0x" + "4a" * 20
    path = _write(
        tmp_path,
        [
            {
                "address": PROXY,
                "facetSrc": ["*.evm"],
                "deployments": {
                    "314": {
                        "current": {
                            "gitCommit": "e" * 40,
                            "facets": {
                                "a.evm": {
                                    "address": "0x" + "ab" * 20,
                                    "codehash": "0x" + "11" * 32,
                                    "initcodeHash": "0x" + "00" * 32,  # forces redeploy
                                    "from": deployer,
                                }
                            },
                        }
                    }
                },
            }
        ],
    )

    deploy.run_deploy(path)

    facet = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]["facets"]["a.evm"]
    assert facet["address"] == "0x" + f"{1:040x}"  # actually redeployed
    assert facet["from"] == deployer


def test_run_deploy_records_from_when_runtime_depends_on_sender(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])
    monkeypatch.setattr(deploy, "deployer_derived", lambda initcode, sender: True)
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    facet = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]["facets"]["a.evm"]
    assert facet["from"] == DEPLOYER  # the actual cast-receipt sender, not a recorded value


def test_run_deploy_omits_from_when_runtime_is_sender_independent(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])  # stub_chain defaults deployer_derived -> False
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    facet = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]["facets"]["a.evm"]
    assert "from" not in facet


def test_run_deploy_verifies_new_sol_facets_on_sourcify(stub_chain, monkeypatch, tmp_path):
    monkeypatch.setattr(
        deploy, "resolve_facets", lambda facet_src, root: [_facet("a.sol:A")]
    )
    calls = []
    monkeypatch.setattr(
        deploy,
        "verify_sourcify",
        lambda facet, address, chain, root: calls.append((facet.source_id, address, chain)),
    )
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.sol"]}])

    deploy.run_deploy(path)

    assert calls == [("a.sol:A", "0x" + f"{1:040x}", "314")]


def test_run_deploy_does_not_reverify_reused_sol_facets(stub_chain, monkeypatch, tmp_path):
    monkeypatch.setattr(
        deploy, "resolve_facets", lambda facet_src, root: [_facet("a.sol:A")]
    )
    calls = []
    monkeypatch.setattr(deploy, "verify_sourcify", lambda *a, **k: calls.append(a))
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.sol"]}])

    deploy.run_deploy(path)  # first run deploys + verifies
    deploy.run_deploy(path)  # nothing changed: reused, must not re-verify

    assert len(calls) == 1


def test_verify_sourcify_calls_forge(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "run", lambda cmd, root: calls.append(cmd) or "")

    deploy.verify_sourcify(_facet("a.sol:A"), A1, "314", ".")

    assert calls == [["forge", "verify-contract", A1, "a.sol:A", "--chain", "314", "--verifier", "sourcify"]]


def test_verify_sourcify_failure_is_non_fatal(monkeypatch, capsys):
    def fail(cmd, root):
        raise deploy.click.ClickException("boom")

    monkeypatch.setattr(deploy, "run", fail)

    deploy.verify_sourcify(_facet("a.sol:A"), A1, "314", ".")  # must not raise

    assert "warning: Sourcify verification failed" in capsys.readouterr().err


def test_run_deploy_requires_rpc_url(monkeypatch, tmp_path):
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    path = _write(tmp_path, [])
    with pytest.raises(Exception, match="ETH_RPC_URL"):
        deploy.run_deploy(path)


# -- migration script -------------------------------------------------------


class FakeStorage:
    """Stand-in for ProxyStorage: slot = keccak-ish of the selector, value = 0."""

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
    from josuke import selectors as _s
    from josuke import delegate as _d

    sm, dm = dict(_s.selector_map), dict(_d.source_map)
    _s.selector_map.clear()
    _d.source_map.clear()
    yield
    _s.selector_map.clear()
    _s.selector_map.update(sm)
    _d.source_map.clear()
    _d.source_map.update(dm)


def _facet(source_id):
    return deploy.facet_from_source_id(source_id)


def test_build_migration_installs_every_proposed_selector(monkeypatch):
    monkeypatch.setattr(deploy, "ProxyStorage", FakeStorage)
    monkeypatch.setattr(
        deploy,
        "facet_selectors",
        lambda facet, root: {"a.sol:A": [_sel("0x11111111")], "b.sol:B": [_sel("0x22222222")]}[facet.source_id],
    )
    facets = [_facet("a.sol:A"), _facet("b.sol:B")]
    proposed_facets = {
        "a.sol:A": {"address": A1},
        "b.sol:B": {"address": B2},
    }

    migration = deploy.build_migration(PROXY, facets, proposed_facets, {}, ".")
    from josuke.migration import Migration

    frags = Migration.decode(migration.encode()).setdelegates
    assert {f.selector for f in frags} == {"0x11111111", "0x22222222"}
    assert {f.delegate.address for f in frags} == {A1, B2}


def test_build_migration_zeroes_dropped_selectors(monkeypatch):
    live_value = "0x" + "00" * 12 + "cd" * 20  # a non-zero delegate currently set
    monkeypatch.setattr(
        deploy, "ProxyStorage", lambda addr: FakeStorage(addr, {"0x99999999": live_value})
    )

    def selectors(facet, root):
        if facet.source_id == "keep.sol:Keep":
            return [_sel("0x11111111")]
        return [_sel("0x99999999")]  # the old facet, only in `current`

    monkeypatch.setattr(deploy, "facet_selectors", selectors)

    facets = [_facet("keep.sol:Keep")]
    proposed_facets = {"keep.sol:Keep": {"address": A1}}
    current = {"facets": {"keep.sol:Keep": {}, "old.sol:Old": {}}}

    migration = deploy.build_migration(PROXY, facets, proposed_facets, current, ".")
    from josuke.migration import Migration

    frags = Migration.decode(migration.encode()).setdelegates
    by_sel = {f.selector: f for f in frags}
    assert by_sel["0x99999999"].delegate.address.lower() == deploy.ZERO_ADDRESS
    assert by_sel["0x11111111"].delegate.address == A1


def test_build_migration_none_when_dropped_selector_already_clear(monkeypatch):
    monkeypatch.setattr(deploy, "ProxyStorage", FakeStorage)  # all values zero

    def selectors(facet, root):
        return [_sel("0x99999999")] if "old" in facet.source_id else []

    monkeypatch.setattr(deploy, "facet_selectors", selectors)
    current = {"facets": {"old.sol:Old": {}}}

    assert deploy.build_migration(PROXY, [], {}, current, ".") is None


def test_build_migration_none_when_already_routed(monkeypatch):
    """Re-running deploy with nothing changed (e.g. right after accept) must not
    propose a migration: every selector already routes to its recorded facet."""
    word = "0x" + "00" * 12 + A1[2:].lower()
    monkeypatch.setattr(
        deploy, "ProxyStorage", lambda addr: FakeStorage(addr, {"0x11111111": word})
    )
    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel("0x11111111")])
    facets = [_facet("a.sol:A")]
    proposed_facets = {"a.sol:A": {"address": A1}}
    current = {"facets": {"a.sol:A": {"address": A1}}}

    assert deploy.build_migration(PROXY, facets, proposed_facets, current, ".") is None


def test_build_migration_rejects_selector_clash(monkeypatch):
    monkeypatch.setattr(deploy, "ProxyStorage", FakeStorage)
    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel("0x11111111")])
    facets = [_facet("a.sol:A"), _facet("b.sol:B")]
    proposed_facets = {"a.sol:A": {"address": A1}, "b.sol:B": {"address": B2}}

    with pytest.raises(Exception, match="claimed by"):
        deploy.build_migration(PROXY, facets, proposed_facets, {}, ".")


def test_build_migration_rejects_duplicate_storage_slot(monkeypatch):
    class CollidingStorage(FakeStorage):
        def fetch(self, selectors):
            for s in selectors:  # slot detection returns the same slot for every selector
                self.storage_keys[s.selector] = "0x" + "00" * 31 + "07"
                self.storage_values.setdefault(s.selector, "0x" + "00" * 32)

    monkeypatch.setattr(deploy, "ProxyStorage", CollidingStorage)
    monkeypatch.setattr(
        deploy,
        "facet_selectors",
        lambda facet, root: {"a.sol:A": [_sel("0x11111111")], "b.sol:B": [_sel("0x22222222")]}[facet.source_id],
    )
    facets = [_facet("a.sol:A"), _facet("b.sol:B")]
    proposed_facets = {"a.sol:A": {"address": A1}, "b.sol:B": {"address": B2}}

    with pytest.raises(Exception, match="storage slot"):
        deploy.build_migration(PROXY, facets, proposed_facets, {}, ".")


def test_build_migration_routes_generated_selectors(monkeypatch):
    from josuke.erc8167 import SELECTORS_SELECTOR
    from josuke.migration import Migration

    monkeypatch.setattr(deploy, "ProxyStorage", FakeStorage)
    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel("0x11111111")])
    facets = [_facet("a.sol:A")]
    proposed_facets = {"a.sol:A": {"address": A1}}

    migration = deploy.build_migration(
        PROXY, facets, proposed_facets, {}, ".", selectors_impl={"address": B2}
    )
    frags = {sd.selector: sd for sd in Migration.decode(migration.encode()).setdelegates}
    assert frags[SELECTORS_SELECTOR].delegate.address == B2


# -- selectors() synthesis -------------------------------------------------


def test_selectors_runtime_defers_to_a_facet_that_implements_it(monkeypatch):
    from josuke.erc8167 import SELECTORS_SELECTOR

    monkeypatch.setattr(
        deploy, "facet_selectors", lambda facet, root: [_sel(SELECTORS_SELECTOR)]
    )
    assert deploy.selectors_runtime([_facet("a.sol:A")], ".") is None


def test_selectors_runtime_generates_from_the_facet_set(monkeypatch):
    from josuke.erc8167 import generated_selectors, selectors_method

    monkeypatch.setattr(deploy, "facet_selectors", lambda facet, root: [_sel("0x11111111")])
    runtime = deploy.selectors_runtime([_facet("a.sol:A")], ".")
    assert runtime == selectors_method(generated_selectors([[_sel("0x11111111")]]))


def test_run_deploy_synthesizes_and_records_selectors(stub_chain, monkeypatch, tmp_path, capsys):
    _resolve_to(monkeypatch, ["a.evm"])
    monkeypatch.setattr(deploy, "selectors_runtime", lambda facets, root: b"\x60\x00")
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    proposed = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert "address" in proposed["selectors"]
    assert b"\x60\x00".hex() in stub_chain["deployed"]  # deployed with the universal constructor
    assert "selectors() generated" in capsys.readouterr().out


def test_run_deploy_omits_selectors_when_a_facet_owns_it(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])
    monkeypatch.setattr(deploy, "selectors_runtime", lambda facets, root: None)
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    proposed = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert "selectors" not in proposed


def test_run_deploy_reuses_selectors_impl_when_runtime_unchanged(stub_chain, monkeypatch, tmp_path, capsys):
    _resolve_to(monkeypatch, ["a.evm"])
    monkeypatch.setattr(deploy, "selectors_runtime", lambda facets, root: b"\x60\x00")
    monkeypatch.setattr(deploy, "eth_get_code", lambda address: "0x6000")
    prior_impl = {"address": "0x" + "5e" * 20}
    path = _write(
        tmp_path,
        [
            {
                "address": PROXY,
                "facetSrc": ["*.evm"],
                "deployments": {
                    "314": {
                        "proposed": {
                            "gitCommit": "e" * 40,
                            "facets": {"a.evm": {"address": "0x" + "ab" * 20, "codehash": "0x" + "11" * 32, "initcodeHash": keccak_hex("a.evm".encode().hex())}},
                            "selectors": prior_impl,
                        }
                    }
                },
            }
        ],
    )

    deploy.run_deploy(path)

    proposed = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert proposed["selectors"] == prior_impl
    assert b"\x60\x00".hex() not in stub_chain["deployed"]
    assert "selectors() unchanged" in capsys.readouterr().out


def test_deploy_migration_reuses_prior_when_code_matches(monkeypatch):
    from josuke.migration import Migration

    monkeypatch.setattr(deploy, "universal_constructor", lambda: "600b")
    runtime = b"\x00" * 100

    class M(Migration):
        def encode(self):
            return runtime

    prior = {"address": "0x" + "ab" * 20}
    monkeypatch.setattr(deploy, "eth_get_code", lambda address: "0x" + runtime.hex())
    monkeypatch.setattr(deploy, "deploy_initcode", lambda *a: pytest.fail("should not deploy"))

    assert deploy.deploy_migration(M([]), prior, ".") is prior


def test_deploy_migration_deploys_when_no_prior(monkeypatch):
    from josuke.migration import Migration

    seen = {}
    monkeypatch.setattr(deploy, "universal_constructor", lambda: "600b")

    def fake_deploy(initcode, root):
        seen["initcode"] = initcode
        return "0x" + "77" * 20, DEPLOYER

    monkeypatch.setattr(deploy, "deploy_initcode", fake_deploy)

    class M(Migration):
        def encode(self):
            return b"\xaa" * 100

    out = deploy.deploy_migration(M([]), None, ".")
    assert out == {"address": "0x" + "77" * 20}
    assert seen["initcode"] == "600b" + "aa" * 100  # universal constructor + runtime


def test_run_deploy_records_migration(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])

    class FakeMigration:
        def encode(self):
            return b"\x00" * 100

    monkeypatch.setattr(deploy, "build_migration", lambda *a, **k: FakeMigration())
    monkeypatch.setattr(
        deploy, "deploy_migration", lambda m, prior, root: {"address": "0x" + "99" * 20}
    )
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    proposed = json.loads(path.read_text())[0]["deployments"]["314"]["proposed"]
    assert proposed["migration"] == {"address": "0x" + "99" * 20}


def test_run_deploy_omits_proposed_when_no_migration_needed(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])
    monkeypatch.setattr(deploy, "build_migration", lambda *a, **k: None)
    path = _write(tmp_path, [{"address": PROXY, "facetSrc": ["*.evm"]}])

    deploy.run_deploy(path)

    history = json.loads(path.read_text())[0]["deployments"]["314"]
    assert "proposed" not in history


def test_run_deploy_clears_stale_proposed_when_no_migration_needed(stub_chain, monkeypatch, tmp_path):
    _resolve_to(monkeypatch, ["a.evm"])
    monkeypatch.setattr(deploy, "build_migration", lambda *a, **k: None)
    path = _write(
        tmp_path,
        [
            {
                "address": PROXY,
                "facetSrc": ["*.evm"],
                "deployments": {
                    "314": {
                        "proposed": {
                            "gitCommit": "e" * 40,
                            "facets": {"a.evm": {"address": "0x" + "ab" * 20, "codehash": "0x" + "11" * 32, "initcodeHash": "0x" + "00" * 32}},
                            "migration": {"address": "0x" + "77" * 20},
                        }
                    }
                },
            }
        ],
    )

    deploy.run_deploy(path)

    history = json.loads(path.read_text())[0]["deployments"]["314"]
    assert "proposed" not in history
