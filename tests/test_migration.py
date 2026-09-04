import pytest

from josuke.delegate import ContractSource, Delegate
from josuke.migration import (
    FINAL_BLOCK_SIZE,
    InvalidMigration,
    Migration,
    NORMAL_BLOCK_SIZE,
    SELECTOR_DELEGATED_SIG,
    SetDelegate,
)
from josuke.opcodes import PUSH20, PUSH32
from eth_utils import to_checksum_address

ADDR_A = to_checksum_address("0x" + "a1" * 20)
ADDR_B = to_checksum_address("0x" + "b2" * 20)
KEY = "0x035e8a4aa5c458e1b67ba99aeadf723b02971482f900ce17c7ae150613e85d24"
KEY2 = "0x1111111111111111111111111111111111111111111111111111111111111111"


def _sd(selector4, address, key=KEY):
    delegate = Delegate(address, ContractSource("src/F.sol", "F"))
    return SetDelegate("0x" + selector4, key, delegate)


def test_migration_encode_pushes_sig_once_and_delegate_once_per_facet():
    a1, a2 = _sd("11111111", ADDR_A), _sd("22222222", ADDR_A, KEY2)
    b1 = _sd("33333333", ADDR_B)
    encoded = Migration([a1, a2, b1]).encode()

    # sig: leading PUSH32, and it never appears again as a PUSH32 immediate
    assert encoded[:1].hex() == PUSH32
    assert encoded[1:33] == SELECTOR_DELEGATED_SIG
    assert SELECTOR_DELEGATED_SIG not in encoded[33:]

    # each facet address is pushed exactly once
    assert encoded.count(bytes.fromhex(PUSH20 + ADDR_A.removeprefix("0x").lower())) == 1
    assert encoded.count(bytes.fromhex(PUSH20 + ADDR_B.removeprefix("0x").lower())) == 1

    # 33 (sig) + per facet: 21 (PUSH20) + NORMAL per non-last + FINAL for the last
    expected_len = 33 + (21 + NORMAL_BLOCK_SIZE + FINAL_BLOCK_SIZE) + (21 + FINAL_BLOCK_SIZE)
    assert len(encoded) == expected_len


def test_migration_round_trips():
    sds = [
        _sd("11111111", ADDR_A),
        _sd("22222222", ADDR_A, KEY2),
        _sd("33333333", ADDR_B),
    ]
    decoded = Migration.decode(Migration(sds).encode())

    assert {sd.selector for sd in decoded.setdelegates} == {
        "0x11111111", "0x22222222", "0x33333333"
    }
    by_sel = {sd.selector: sd for sd in decoded.setdelegates}
    assert by_sel["0x11111111"].delegate.address == ADDR_A
    assert by_sel["0x33333333"].delegate.address == ADDR_B
    assert by_sel["0x22222222"].storage_key32 == KEY2.removeprefix("0x")


def test_migration_round_trips_single_selector_facet():
    encoded = Migration([_sd("11111111", ADDR_A)]).encode()
    # a one-selector group is just the sig, the PUSH20 and a single FINAL block
    assert len(encoded) == 33 + 21 + FINAL_BLOCK_SIZE
    (sd,) = Migration.decode(encoded).setdelegates
    assert sd.selector == "0x11111111" and sd.delegate.address == ADDR_A


def test_migration_round_trips_group_of_three():
    # one facet, three selectors: two NORMAL blocks then a FINAL block, so
    # consecutive NORMAL blocks and the NORMAL->FINAL boundary both get parsed.
    keys = ["0x" + f"{n:02x}" * 32 for n in (1, 2, 3)]
    sds = [_sd(s, ADDR_A, k) for s, k in zip(("11111111", "22222222", "33333333"), keys)]
    encoded = Migration(sds).encode()
    assert len(encoded) == 33 + 21 + 2 * NORMAL_BLOCK_SIZE + FINAL_BLOCK_SIZE

    by_sel = {sd.selector: sd for sd in Migration.decode(encoded).setdelegates}
    assert set(by_sel) == {"0x11111111", "0x22222222", "0x33333333"}
    assert all(by_sel[s].delegate.address == ADDR_A for s in by_sel)
    assert {by_sel[s].storage_key32 for s in by_sel} == {k.removeprefix("0x") for k in keys}


def test_decode_rejects_corruption():
    encoded = Migration([_sd("11111111", ADDR_A), _sd("22222222", ADDR_A, KEY2)]).encode()
    assert Migration.decode(encoded).setdelegates  # the untouched encoding round-trips

    with pytest.raises(InvalidMigration):
        Migration.decode(encoded[:-1])  # truncated

    with pytest.raises(InvalidMigration):
        Migration.decode(encoded + b"\x00")  # trailing junk

    with pytest.raises(InvalidMigration):
        Migration.decode(b"\x00" + encoded[1:])  # not a leading PUSH32

    mutated = bytearray(encoded)
    mutated[10] ^= 0xFF  # a byte inside the event sig
    with pytest.raises(InvalidMigration):
        Migration.decode(bytes(mutated))

    mutated = bytearray(encoded)
    mutated[54] = 0x00  # the DUP1 opening the first selector block (after PUSH20 + 20-byte addr)
    with pytest.raises(InvalidMigration):
        Migration.decode(bytes(mutated))


def test_decode_rejects_unknown_delegate():
    encoded = Migration([_sd("11111111", ADDR_A)]).encode()
    mutated = bytearray(encoded)
    mutated[34:54] = bytes.fromhex("de" * 20)  # a PUSH20 immediate with no Delegate
    with pytest.raises((InvalidMigration, KeyError)):
        Migration.decode(bytes(mutated))
