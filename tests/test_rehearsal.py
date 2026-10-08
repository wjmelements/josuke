"""Rehearsals run the real `evm -nx` against a mock node holding an ERC-8167 proxy."""

from unittest.mock import patch

import pytest
from eth_utils import to_checksum_address

from ethrpc_mock import MockEthRpc
from reference_proxy import PROXY_CODE, mapping_slot as slot, prefixed_proxy, word
from josuke import deploy, rehearsal, verify
from josuke.erc8167 import SELECTORS_SELECTOR
from josuke.delegate import ContractSource, Delegate
from josuke.migration import Migration, SetDelegate
from josuke.opcodes import CALLDATALOAD, POP, PUSH0, PUSH1, SHR, SLOAD
from josuke.rehearsal import rehearse_migration
from josuke.selectors import Selector
from josuke.storage import ProxyStorage

PROXY = to_checksum_address("0x" + "1a" * 20)
# The reference proxy, first reading the slot numbered by the selector itself.
SELECTOR_SLOT_PROXY_CODE = prefixed_proxy(f"{PUSH0}{CALLDATALOAD}{PUSH1}e0{SHR}{SLOAD}{POP}")

ADD, KEEP, DROP = "0x11111111", "0x22222222", "0x33333333"
OLD = to_checksum_address("0x" + "0d" * 20)
NEW = to_checksum_address("0x" + "0e" * 20)
SELECTORS_IMPL = to_checksum_address("0x" + "5e" * 20)
MIGRATION = to_checksum_address("0x" + "4d" * 20)


def sel(selector: str) -> Selector:
    return Selector(selector, f"f{selector}()")


def migration(*routes) -> bytes:
    return Migration([SetDelegate(s, k, Delegate(a, ContractSource("", ""))) for s, k, a in routes]).encode()


@pytest.fixture
def chain(monkeypatch):
    """KEEP and DROP route to OLD; ADD is new."""
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc()
    rpc.set_code(PROXY, PROXY_CODE)
    rpc.set_storage(PROXY, slot(KEEP), word(OLD))
    rpc.set_storage(PROXY, slot(DROP), word(OLD))
    monkeypatch.setattr(rehearsal, "eth_get_code", lambda address: rpc.code[address.lower()])
    with patch("josuke.ethjsonrpc.post", rpc):
        yield rpc


ROUTES = {ADD: NEW, KEEP: OLD, DROP: None}


@pytest.mark.timeout(5)
def test_correct_migration_passes(chain):
    runtime = migration((ADD, slot(ADD), NEW), (DROP, slot(DROP), "0x" + "00" * 20))
    assert rehearse_migration(PROXY, runtime, ROUTES, {OLD, NEW}) == []


@pytest.mark.timeout(5)
def test_wrong_slots_are_caught_by_the_dispatcher(chain):
    runtime = migration((ADD, slot(KEEP), NEW), (DROP, "0x" + "00" * 32, "0x" + "00" * 20))
    assert rehearse_migration(PROXY, runtime, ROUTES, {OLD, NEW}) == [
        f"{ADD} reverts, expected {NEW}",
        f"{KEEP} routes to {NEW}, expected {OLD}",
        f"{DROP} routes to {OLD}, expected removal",
    ]


@pytest.mark.timeout(5)
def test_reverting_migration_fails(chain):
    assert rehearse_migration(PROXY, bytes.fromhex("5f5ffd"), ROUTES, {OLD, NEW}) == ["migration reverted"]


@pytest.mark.timeout(5)
def test_slot_detection_passes_an_earlier_sload_keyed_by_selector(chain):
    chain.set_code(PROXY, SELECTOR_SLOT_PROXY_CODE)
    storage = ProxyStorage(PROXY)
    storage.fetch([sel(ADD)])
    assert storage.storage_keys[ADD] == slot(ADD)

    runtime = migration((ADD, storage.storage_keys[ADD], NEW))
    assert rehearse_migration(PROXY, runtime, {ADD: NEW}, {NEW}) == []


def _own(monkeypatch, selectors_by_source):
    monkeypatch.setattr(
        deploy, "facet_selectors", lambda facet, root: [sel(s) for s in selectors_by_source[facet.source_id]]
    )


def test_migration_routes(monkeypatch):
    _own(monkeypatch, {"keep.sol:Keep": [ADD, KEEP], "old.sol:Old": [DROP]})
    current = {"facets": {"keep.sol:Keep": {"address": OLD}, "old.sol:Old": {"address": OLD}}}
    proposed = {"facets": {"keep.sol:Keep": {"address": NEW}}, "selectors": {"address": SELECTORS_IMPL}}

    routes, delegates = deploy.migration_routes(proposed, current, ".", ".")

    assert routes == {ADD: NEW, KEEP: NEW, SELECTORS_SELECTOR: SELECTORS_IMPL, DROP: None}
    assert delegates == {OLD, NEW, SELECTORS_IMPL}


@pytest.mark.timeout(5)
def test_verify_flags_an_onchain_migration_that_writes_a_wrong_slot(chain, monkeypatch):
    _own(monkeypatch, {"add.sol:Add": [ADD], "keep.sol:Keep": [KEEP], "drop.sol:Drop": [DROP]})
    current = {"facets": {"keep.sol:Keep": {"address": OLD}, "drop.sol:Drop": {"address": OLD}}}
    proposed = {
        "facets": {"add.sol:Add": {"address": NEW}, "keep.sol:Keep": {"address": OLD}},
        "migration": {"address": MIGRATION},
    }
    # A well-formed migration script, so decoding it succeeds, but ADD's route lands in KEEP's slot.
    code = {MIGRATION.lower(): "0x" + migration((ADD, slot(KEEP), NEW), (DROP, slot(DROP), "0x" + "00" * 20)).hex()}
    monkeypatch.setattr(verify, "eth_get_code", lambda address: code[address.lower()])
    report = verify.Report()

    verify.verify_rehearsal(PROXY, proposed, current, ".", ".", report)

    assert report.failures == [
        f" proposed.migration rehearsal: {ADD} reverts, expected {NEW}",
        f" proposed.migration rehearsal: {KEEP} routes to {NEW}, expected {OLD}",
    ]


@pytest.mark.timeout(5)
def test_rehearsal_reuses_what_slot_detection_fetched(chain):
    cache = {}
    ProxyStorage(PROXY, cache=cache).fetch([sel(ADD), sel(KEEP), sel(DROP)])
    fetched = len(chain.calls)

    runtime = migration((ADD, slot(ADD), NEW), (DROP, slot(DROP), "0x" + "00" * 20))
    assert rehearse_migration(PROXY, runtime, ROUTES, {OLD, NEW}, cache) == []

    # Same block, same dispatch slots: nothing about the proxy is fetched again.
    assert not [
        req for payload in chain.calls[fetched:] for req in (payload if isinstance(payload, list) else [payload])
        if req["method"] in ("eth_blockNumber", "eth_getStorageAt") or req["params"][0] == PROXY.lower()
    ]
