from collections import defaultdict
from unittest.mock import patch

import click
import pytest
from eth_utils import keccak

from ethrpc_mock import MockEthRpc
from reference_proxy import PROXY_CODE as ERC8167_PROXY_CODE, mapping_slot, prefixed_proxy, word
from josuke.opcodes import (
    ADD, CALLDATACOPY, CALLDATALOAD, CALLDATASIZE, DELEGATECALL, DUP1, DUP5, EXTCODEHASH, EXTCODESIZE, GAS, ISZERO,
    JUMP, JUMPDEST, JUMPI, MSIZE, MSTORE, POP, PUSH0, PUSH1, PUSH4, PUSH32, RETURN, RETURNDATACOPY, RETURNDATASIZE,
    REVERT, SHA3, SHR, SLOAD, STOP, SWAP1, XOR,
)
from josuke.selectors import Selector
from josuke.storage import ProxyStorage


PROXY_ADDRESS = "0x1A4E1a4e1A4E1a4e1a4E1a4e1A4e1A4E1a4E1A4e"

PROXY_CODE = (
    "5f5f365f60045f5f3760405f2054806023575f51602052635416eb985f526024601c"
    "fd5b365f5f375af43d5f5f3e6034573d5ffd5b3d5ff3"
)

BLOCK_NUMBER = 0x1312D00

ABI = [
  {
    "type": "function",
    "name": "cancelPendingWeight",
    "inputs": [
      {
        "name": "op",
        "type": "uint8",
        "internalType": "enum PendingOp"
      }
    ],
    "outputs": [],
    "stateMutability": "nonpayable"
  },
  {
    "type": "function",
    "name": "quarterlyGateCheck",
    "inputs": [],
    "outputs": [],
    "stateMutability": "nonpayable"
  },
]


@pytest.fixture
def eth_rpc(monkeypatch):
    # Patches the RPC `post` used by the `EvmRelay` behind ProxyStorage.fetch.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc(block_number=BLOCK_NUMBER)
    rpc.set_code(PROXY_ADDRESS, PROXY_CODE)
    with patch("josuke.ethjsonrpc.post", rpc):
        yield rpc


@pytest.mark.timeout(2)
def test_fetch(eth_rpc):
    # PROXY_CODE keeps mapping(bytes4 => address) at slot 0, with nothing linked.
    selectors = [Selector.from_abi(entry) for entry in ABI]
    storage = ProxyStorage(PROXY_ADDRESS)

    storage.fetch(selectors)

    assert eth_rpc.requests_for("eth_getCode")[0]["params"][0] == PROXY_ADDRESS.lower()
    assert storage.storage_keys == {
        s.selector: "0x" + keccak(bytes.fromhex(s.selector[2:].ljust(64, "0")) + bytes(32)).hex() for s in selectors
    }
    assert storage.storage_values == {s.selector: "0x" + "00" * 32 for s in selectors}


@pytest.mark.timeout(2)
def test_fetch_batches_the_routes_and_runs_neither_stub_nor_delegate(eth_rpc):
    selectors = [f"0x{i:08x}" for i in range(1, 6)]
    eth_rpc.set_code(PROXY_ADDRESS, ERC8167_PROXY_CODE)
    eth_rpc.set_code(DELEGATE, "00")
    for selector in selectors:
        eth_rpc.set_storage(PROXY_ADDRESS, mapping_slot(selector), word(DELEGATE))

    ProxyStorage(PROXY_ADDRESS).fetch([Selector(s, f"f{s}()") for s in selectors])

    posts = [post if isinstance(post, list) else [post] for post in eth_rpc.calls]
    assert [len(post) for post in posts if post[0]["method"] == "eth_getStorageAt"] == [len(selectors)]
    assert {req["params"][0] for post in posts for req in post if req["params"]} == {PROXY_ADDRESS.lower()}


# keccak("eip1967.proxy.implementation") - 1, where ERC-1822 kept its implementation too
IMPLEMENTATION_SLOT = "360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
# The reference proxy, first reading the implementation slot it was migrated from.
MIGRATED_PROXY_CODE = prefixed_proxy(f"{PUSH32}{IMPLEMENTATION_SLOT}{SLOAD}{POP}")
XOR_KEY = "aa" * 32
# Reads slot 0, then delegates to the address at msg.sig ^ XOR_KEY: a key that no
# storage layout convention derives from the selector.
XOR_LOOKUP = "5f5450" "5f3560e01c7f" + XOR_KEY + "1854"
FORWARD = "365f5f37" "5f5f365f845af4" "3d5f5f3e" "3d5ff3"  # DELEGATECALL the address on the stack
XOR_PROXY_CODE = XOR_LOOKUP + FORWARD
# Solidity's existence check before an external or library call that expects no return
# data, `if iszero(extcodesize(addr)) { revert(0, 0) }` (the reason string is only
# with --revert-strings debug), ahead of the same forward.
CHECKED_TAG = len(XOR_LOOKUP) // 2 + 8
CHECKED_PROXY_CODE = (
    XOR_LOOKUP
    + f"{DUP1}{EXTCODESIZE}{PUSH1}{CHECKED_TAG:02x}{JUMPI}{PUSH0}{PUSH0}{REVERT}{JUMPDEST}"
    + FORWARD
)
# XOR_PROXY_CODE, also taking the code hash of the address at slot 0.
HASHING_PROXY_CODE = f"{PUSH0}{SLOAD}{EXTCODEHASH}{POP}" + XOR_LOOKUP[6:] + FORWARD
CODELESS = "0x" + "0b" * 20

LINKED, UNLINKED = "0x7a7a7a7a", "0x7b7b7b7b"
DELEGATE = "0x" + "0d" * 20
OLD_IMPLEMENTATION = "0x" + "0c" * 20


def xor_slot(selector: str) -> str:
    return f"0x{int(selector, 16) ^ int(XOR_KEY, 16):064x}"


def probe(code: str, storage: dict, selectors: list[str], codes: dict | None = None) -> ProxyStorage:
    rpc = MockEthRpc(block_number=BLOCK_NUMBER)
    rpc.set_code(PROXY_ADDRESS, code)
    rpc.set_code(DELEGATE, "60075400")  # reads proxy slot 7 itself
    rpc.set_code(OLD_IMPLEMENTATION, "00")
    for address, runtime in (codes or {}).items():
        rpc.set_code(address, runtime)
    for key, value in storage.items():
        rpc.set_storage(PROXY_ADDRESS, key, value)
    proxy_storage = ProxyStorage(PROXY_ADDRESS)
    with patch("josuke.ethjsonrpc.post", rpc):
        proxy_storage.fetch([Selector(s, f"f{s}()") for s in selectors])
    return proxy_storage


@pytest.mark.timeout(5)
def test_finds_the_mapping_behind_an_erc1822_implementation_slot(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(
        MIGRATED_PROXY_CODE,
        {"0x" + IMPLEMENTATION_SLOT: word(OLD_IMPLEMENTATION), mapping_slot(LINKED): word(DELEGATE)},
        [LINKED, UNLINKED],
    )

    assert storage.storage_keys == {LINKED: mapping_slot(LINKED), UNLINKED: mapping_slot(UNLINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE), UNLINKED: "0x" + "00" * 32}


@pytest.mark.timeout(5)
def test_finds_the_read_it_delegates_to(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(XOR_PROXY_CODE, {"0x0": word(OLD_IMPLEMENTATION), xor_slot(LINKED): word(DELEGATE)}, [LINKED])

    assert storage.storage_keys == {LINKED: xor_slot(LINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE)}


@pytest.mark.timeout(5)
def test_passes_a_slot_every_selector_reads(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(XOR_PROXY_CODE, {"0x0": word(OLD_IMPLEMENTATION)}, [LINKED, UNLINKED])

    assert storage.storage_keys == {LINKED: xor_slot(LINKED), UNLINKED: xor_slot(UNLINKED)}


@pytest.mark.timeout(5)
def test_fails_when_no_read_routes(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    with pytest.raises(click.ClickException, match=f"routes {LINKED} to a delegate"):
        probe("5f545000", {}, [LINKED, UNLINKED])  # both read only slot 0


FACET_ADDRESS_KEY = b"facetAddress"
PAUSED_KEY = b"paused"


def metadata_read(key: bytes) -> str:
    """With selectorInfo[msg.sig] on the stack, SLOAD selectorInfo[msg.sig][key]:
    keccak(key . selectorInfo[msg.sig]), the preimage built at memory 0x40."""
    return (
        f"{PUSH32}{key.hex().ljust(64, '0')}{PUSH1}40{MSTORE}"  # key at 0x40
        f"{DUP1}{PUSH1}{0x40 + len(key):02x}{MSTORE}"  # selectorInfo[msg.sig] right after it
        f"{PUSH1}{len(key) + 32:02x}{PUSH1}40{SHA3}{SLOAD}"
    )


# mapping(bytes4 selector => mapping(string key => bytes32 value) facetMetadata) selectorInfo
# at slot 0. It reads selectorInfo[msg.sig][PAUSED_KEY], then delegates to
# selectorInfo[msg.sig][FACET_ADDRESS_KEY].
METADATA_PROXY_CODE = (
    f"{PUSH1}04{PUSH0}{PUSH0}{CALLDATACOPY}"  # msg.sig at [0:4)
    f"{PUSH1}40{PUSH0}{SHA3}"  # selectorInfo[msg.sig] = keccak(msg.sig . 0)
    + metadata_read(PAUSED_KEY) + POP
    + metadata_read(FACET_ADDRESS_KEY)
    + f"{CALLDATASIZE}{PUSH0}{PUSH0}{CALLDATACOPY}"
    f"{PUSH0}{PUSH0}{CALLDATASIZE}{PUSH0}{DUP5}{GAS}{DELEGATECALL}"
    f"{RETURNDATASIZE}{PUSH0}{PUSH0}{RETURNDATACOPY}"
    f"{RETURNDATASIZE}{PUSH0}{RETURN}"
)


def metadata_slot(selector: str, key: bytes) -> str:
    inner = keccak(bytes.fromhex(selector[2:].ljust(64, "0")) + bytes(32))
    return "0x" + keccak(key + inner).hex()


@pytest.mark.timeout(5)
def test_finds_the_route_after_a_flag_keyed_by_selector(monkeypatch):
    # Both reads are keyed by the selector through a nested mapping.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(
        METADATA_PROXY_CODE,
        {metadata_slot(LINKED, PAUSED_KEY): word("0x01"), metadata_slot(LINKED, FACET_ADDRESS_KEY): word(DELEGATE)},
        [LINKED],
    )

    assert storage.storage_keys == {LINKED: metadata_slot(LINKED, FACET_ADDRESS_KEY)}
    assert storage.storage_values == {LINKED: word(DELEGATE)}


@pytest.mark.timeout(5)
def test_delegation_behind_a_code_existence_check(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(CHECKED_PROXY_CODE, {"0x0": word(OLD_IMPLEMENTATION), xor_slot(LINKED): word(DELEGATE)}, [LINKED])

    assert storage.storage_keys == {LINKED: xor_slot(LINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE)}


@pytest.mark.timeout(5)
def test_code_existence_check_marks_a_route_with_no_code(monkeypatch):
    # The check reverts before any DELEGATECALL, but passes for the stub standing in for it.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(CHECKED_PROXY_CODE, {"0x0": word(OLD_IMPLEMENTATION), xor_slot(LINKED): word(CODELESS)}, [LINKED])

    assert storage.storage_keys == {LINKED: xor_slot(LINKED)}
    assert storage.storage_values == {LINKED: word(CODELESS)}


@pytest.mark.timeout(5)
def test_passes_a_code_check_of_another_slot(monkeypatch):
    # The proxy takes the code hash of slot 0's address, but delegates to the route's.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(HASHING_PROXY_CODE, {"0x0": word(OLD_IMPLEMENTATION), xor_slot(LINKED): word(DELEGATE)}, [LINKED])

    assert storage.storage_keys == {LINKED: xor_slot(LINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE)}


FUNCTION_NOT_FOUND = "5416eb98"  # FunctionNotFound(bytes4)
# bool paused; address[2**32] selectorToDelegate;
# require(!paused); then selectorToDelegate[uint32(msg.sig)], at slot 1 + uint32(msg.sig),
# reverting FunctionNotFound(msg.sig) when it is address(0).
PAUSE_CHECK = f"{PUSH0}{SLOAD}{ISZERO}{PUSH1}09{JUMPI}{PUSH0}{PUSH0}{REVERT}{JUMPDEST}"
ARRAY_LOOKUP = f"{PUSH0}{CALLDATALOAD}{PUSH1}e0{SHR}{PUSH1}01{ADD}{SLOAD}"
NOT_FOUND = (
    f"{PUSH1}04{PUSH0}{PUSH1}20{CALLDATACOPY}"  # msg.sig at [0x20:0x24)
    f"{PUSH4}{FUNCTION_NOT_FOUND}{PUSH0}{MSTORE}{PUSH1}24{PUSH1}1c{REVERT}"
)
FOUND_TAG = (len(PAUSE_CHECK) + len(ARRAY_LOOKUP) + len(NOT_FOUND)) // 2 + 4  # past DUP1 PUSH1 tag JUMPI
ARRAY_PROXY_CODE = (
    PAUSE_CHECK + ARRAY_LOOKUP + f"{DUP1}{PUSH1}{FOUND_TAG:02x}{JUMPI}" + NOT_FOUND + JUMPDEST + FORWARD
)


def array_slot(selector: str) -> str:
    return f"0x{1 + int(selector, 16):064x}"


@pytest.mark.timeout(5)
def test_finds_an_array_indexed_by_selector_behind_a_pause_check(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(ARRAY_PROXY_CODE, {array_slot(LINKED): word(DELEGATE)}, [LINKED, UNLINKED])

    assert storage.storage_keys == {LINKED: array_slot(LINKED), UNLINKED: array_slot(UNLINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE), UNLINKED: "0x" + "00" * 32}


# A facet keeping mapping(bytes4 => bool) pausedSelectors at slot 5, which reads
# pausedSelectors[msg.sig] from the proxy's storage once dispatched to.
PAUSABLE = "0x" + "0f" * 20
PAUSABLE_CODE = (
    f"{PUSH1}04{PUSH0}{PUSH0}{CALLDATACOPY}"  # msg.sig at [0:4)
    f"{PUSH1}05{MSIZE}{MSTORE}"  # slot 5 after it
    f"{MSIZE}{PUSH0}{SHA3}{SLOAD}{POP}{STOP}"
)


def paused_slot(selector: str) -> str:
    return "0x" + keccak(bytes.fromhex(selector[2:].ljust(64, "0")) + (5).to_bytes(32, "big")).hex()


@pytest.mark.timeout(5)
def test_ignores_a_facets_own_read_keyed_by_selector(monkeypatch):
    # (1.) The facet's pausedSelectors[msg.sig] is keyed by the selector, the proxy's
    # array index isn't, but only the proxy's read routes.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(
        ARRAY_PROXY_CODE, {array_slot(LINKED): word(PAUSABLE)}, [LINKED, UNLINKED], {PAUSABLE: PAUSABLE_CODE}
    )

    assert storage.storage_keys == {LINKED: array_slot(LINKED), UNLINKED: array_slot(UNLINKED)}
    assert storage.storage_values == {LINKED: word(PAUSABLE), UNLINKED: "0x" + "00" * 32}


@pytest.mark.timeout(5)
def test_finds_an_unlinked_route_behind_a_flag_keyed_by_selector(monkeypatch):
    # (3.) UNLINKED's paused flag and route are both keyed by it, and neither is
    # delegated to, so only the route's role in dispatch tells them apart.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(
        METADATA_PROXY_CODE,
        {
            metadata_slot(LINKED, FACET_ADDRESS_KEY): word(DELEGATE),
            metadata_slot(UNLINKED, PAUSED_KEY): word("0x01"),
        },
        [LINKED, UNLINKED],
    )

    assert storage.storage_keys == {
        LINKED: metadata_slot(LINKED, FACET_ADDRESS_KEY),
        UNLINKED: metadata_slot(UNLINKED, FACET_ADDRESS_KEY),
    }
    assert storage.storage_values == {LINKED: word(DELEGATE), UNLINKED: "0x" + "00" * 32}


ZERO_SELECTOR = "0x00000000"  # a gas-golfed vanity selector
# The reference proxy behind PAUSE_CHECK's `require(!paused)` at slot 0.
PAUSED_ERC8167_PROXY_CODE = prefixed_proxy(PAUSE_CHECK)


@pytest.mark.timeout(5)
def test_a_zero_selector_is_not_slot_zero(monkeypatch):
    # (4.) Slot 0 is numbered by 0x00000000, and any zero-padded preimage contains
    # it, so the pause check's read looks keyed by the selector as well.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(PAUSED_ERC8167_PROXY_CODE, {mapping_slot(LINKED): word(DELEGATE)}, [ZERO_SELECTOR, LINKED])

    assert storage.storage_keys == {ZERO_SELECTOR: mapping_slot(ZERO_SELECTOR), LINKED: mapping_slot(LINKED)}
    assert storage.storage_values == {ZERO_SELECTOR: "0x" + "00" * 32, LINKED: word(DELEGATE)}


DISPATCHER = "0x" + "0a" * 20
# An ERC-1822 proxy delegating every call to the implementation at its slot, here
# the reference proxy, which dispatches through delegates[msg.sig] in the same storage.
ERC1822_PROXY_CODE = f"{PUSH32}{IMPLEMENTATION_SLOT}{SLOAD}" + FORWARD


@pytest.mark.timeout(5)
@pytest.mark.parametrize("selectors", [[LINKED, UNLINKED], [LINKED], [UNLINKED]])
def test_finds_the_route_behind_a_delegated_dispatcher(monkeypatch, selectors):
    # Pointing the implementation slot at the stub routes any selector, even alone.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(
        ERC1822_PROXY_CODE,
        {"0x" + IMPLEMENTATION_SLOT: word(DISPATCHER), mapping_slot(LINKED): word(DELEGATE)},
        selectors,
        {DISPATCHER: ERC8167_PROXY_CODE},
    )

    values = {LINKED: word(DELEGATE), UNLINKED: "0x" + "00" * 32}
    assert storage.storage_keys == {s: mapping_slot(s) for s in selectors}
    assert storage.storage_values == {s: values[s] for s in selectors}


SELECTOR = f"{PUSH0}{CALLDATALOAD}{PUSH1}e0{SHR}"  # msg.sig, as a word


def selector_slot(selector: str) -> str:
    return "0x" + selector[2:].rjust(64, "0")



# A gate at slot 1 that must be set, read before the route at slot msg.sig or after
# it, but checked after both: testing the route while the gate reads as zero fails.
GATE = f"{PUSH1}01{SLOAD}"
ROUTE = SELECTOR + SLOAD
GATE_TAG = len(GATE + ROUTE + f"{SWAP1}{PUSH1}00{JUMPI}{PUSH0}{PUSH0}{REVERT}") // 2
GATE_CHECK = f"{JUMPI}{PUSH0}{PUSH0}{REVERT}{JUMPDEST}"
GATED_PROXY_CODES = {
    "gate before route": GATE + ROUTE + f"{SWAP1}{PUSH1}{GATE_TAG:02x}" + GATE_CHECK + FORWARD,
    "gate after route": ROUTE + GATE + f"{PUSH1}{GATE_TAG - 1:02x}" + GATE_CHECK + FORWARD,
}


@pytest.mark.timeout(5)
@pytest.mark.parametrize("code", GATED_PROXY_CODES.values(), ids=GATED_PROXY_CODES.keys())
def test_finds_the_route_behind_a_gate_checked_after_it(monkeypatch, code):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    storage = probe(code, {"0x1": word(OLD_IMPLEMENTATION), selector_slot(LINKED): word(DELEGATE)}, [LINKED, UNLINKED])

    assert storage.storage_keys == {LINKED: selector_slot(LINKED), UNLINKED: selector_slot(UNLINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE), UNLINKED: "0x" + "00" * 32}


# A layout version at slot 1 picks the route: msg.sig ^ XOR_KEY once it is set, msg.sig
# before. With the version read as zero, the old layout's slot would route too.
VERSION_HEAD = f"{PUSH1}01{SLOAD}" + SELECTOR + f"{SWAP1}{PUSH1}00{JUMPI}{SLOAD}{PUSH1}00{JUMP}"
NEW_LAYOUT_TAG = len(VERSION_HEAD) // 2
NEW_LAYOUT = f"{JUMPDEST}{PUSH32}{XOR_KEY}{XOR}{SLOAD}"
FORWARD_TAG = NEW_LAYOUT_TAG + len(NEW_LAYOUT) // 2
VERSIONED_PROXY_CODE = (
    VERSION_HEAD.replace(f"{PUSH1}00{JUMPI}", f"{PUSH1}{NEW_LAYOUT_TAG:02x}{JUMPI}").replace(
        f"{PUSH1}00{JUMP}", f"{PUSH1}{FORWARD_TAG:02x}{JUMP}"
    )
    + NEW_LAYOUT
    + JUMPDEST
    + FORWARD
)


@pytest.mark.timeout(5)
def test_follows_the_layout_its_version_picks(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    stale = {"0x1": word(OLD_IMPLEMENTATION), selector_slot(LINKED): word(OLD_IMPLEMENTATION)}
    storage = probe(VERSIONED_PROXY_CODE, {**stale, xor_slot(LINKED): word(DELEGATE)}, [LINKED, UNLINKED])

    assert storage.storage_keys == {LINKED: xor_slot(LINKED), UNLINKED: xor_slot(UNLINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE), UNLINKED: "0x" + "00" * 32}


@pytest.mark.timeout(5)
def test_fails_on_a_paused_proxy(monkeypatch):
    # Unpaused, the proxy may dispatch some other way entirely, so no route is found
    # by pretending it is.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    with pytest.raises(click.ClickException, match=f"routes {LINKED} to a delegate"):
        probe(PAUSED_ERC8167_PROXY_CODE, {"0x0": word(OLD_IMPLEMENTATION), mapping_slot(LINKED): word(DELEGATE)},
              [LINKED, UNLINKED])



# x = 0; for (;;) x += 1 + sload(x): every step reads a higher slot, at a distance its
# value sets, so each fetch of the slots read sends the next trace to slots unread.
CHAIN_WALKER = f"{PUSH0}{JUMPDEST}{DUP1}{SLOAD}{ADD}{PUSH1}01{ADD}{PUSH1}01{JUMP}"

@pytest.mark.timeout(5)
def test_runs_no_delegate_reading_endlessly_deeper(monkeypatch):
    # Chasing the delegate's reads would fetch one more slot every round.
    monkeypatch.setenv("ETH_RPC_URL", "http://mock.rpc/test")
    rpc = MockEthRpc(block_number=BLOCK_NUMBER)
    rpc.storage = defaultdict(lambda: "0x" + "00" * 31 + "01")
    rpc.set_code(PROXY_ADDRESS, ERC8167_PROXY_CODE)
    rpc.set_code(DELEGATE, CHAIN_WALKER)
    rpc.set_storage(PROXY_ADDRESS, mapping_slot(LINKED), word(DELEGATE))
    storage = ProxyStorage(PROXY_ADDRESS)
    with patch("josuke.ethjsonrpc.post", rpc):
        storage.fetch([Selector(LINKED, f"f{LINKED}()")])

    assert storage.storage_keys == {LINKED: mapping_slot(LINKED)}
    assert storage.storage_values == {LINKED: word(DELEGATE)}
    assert [req["params"][1] for req in rpc.requests_for("eth_getStorageAt")] == [mapping_slot(LINKED)]
