"""Tests for josuke.broadcast.

`cast` is faked except in the address test, which checks `create_address`
against `cast compute-address`.
"""

import json
import shutil

import click
import pytest
from eth_utils import to_checksum_address

from josuke import broadcast

SENDER = to_checksum_address("0x" + "3b" * 20)


class FakeCast:
    """Answers `cast wallet address`, `nonce`, `send` and `receipt`; records every call."""

    def __init__(self, nonce=5, receipts=None):
        self.calls = []
        self.nonce = nonce
        self.receipts = receipts or {}  # tx hash -> receipt override

    def __call__(self, cmd, root=None, stdin=None, stdin_fd=None):
        self.calls.append((cmd, stdin_fd))
        if cmd[1:3] == ["wallet", "address"]:
            return SENDER + "\n"
        if cmd[1] == "nonce":
            return f"{self.nonce}\n"
        if cmd[1] == "send":
            nonce = int(cmd[cmd.index("--nonce") + 1])
            return f"0xtx{nonce}\n"
        if cmd[1] == "receipt":
            tx_hash = cmd[2]
            nonce = int(tx_hash.removeprefix("0xtx"))
            receipt = {
                "status": "0x1",
                "blockNumber": hex(100 + nonce),
                "contractAddress": broadcast.create_address(SENDER, nonce),
            }
            return json.dumps(receipt | self.receipts.get(tx_hash, {}))
        raise AssertionError(f"unexpected {cmd}")

    def sends(self):
        return [(cmd, fd) for cmd, fd in self.calls if cmd[1] == "send"]


@pytest.fixture
def cast(monkeypatch):
    monkeypatch.delenv("ETH_FROM", raising=False)
    monkeypatch.setattr(broadcast, "cast_password", lambda: ([], None))
    fake = FakeCast()
    monkeypatch.setattr(broadcast, "run", fake)
    return fake


# -- create_address ---------------------------------------------------------------


@pytest.mark.parametrize(
    "nonce, expected",
    [
        # well-known: the first contracts deployed by 0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0
        (0, "0xcd234a471b72ba2f1ccf0a70fcaba648a5eecd8d"),
        (1, "0x343c43a37d37dff08ae8c4a11544c718abb4fcf8"),
        (2, "0xf778b86fa74e846c4f0a1fbd1335fe81c00a0c91"),
        (3, "0xfffd933a0bc612844eaf0c6fe3e5b8e9b6c1d19c"),
    ],
)
def test_create_address_known_vectors(nonce, expected):
    assert broadcast.create_address("0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", nonce).lower() == expected


@pytest.mark.skipif(shutil.which("cast") is None, reason="requires the `cast` binary")
@pytest.mark.parametrize("nonce", [0, 1, 0x7F, 0x80, 0xFF, 0x100, 2**32, 2**64 - 1])
def test_create_address_matches_cast(nonce):
    out = broadcast.run(["cast", "compute-address", SENDER, "--nonce", str(nonce)])
    assert broadcast.create_address(SENDER, nonce) == to_checksum_address(out.split()[-1])


# -- create / send_all --------------------------------------------------------------


def test_create_reserves_nonces_without_sending(cast):
    b = broadcast.Broadcast()
    first = b.create("aa", ".")
    second = b.create("bb", ".")

    assert first == (broadcast.create_address(SENDER, 5), SENDER)
    assert second == (broadcast.create_address(SENDER, 6), SENDER)
    assert [cmd for cmd, _ in cast.calls if cmd[1] == "nonce"] == [["cast", "nonce", SENDER, "--block", "pending"]]
    assert cast.sends() == []


def test_send_all_sends_in_nonce_order_once(cast):
    b = broadcast.Broadcast()
    a1, _ = b.create("aa", ".")
    a2, _ = b.create("bb", ".")

    b.send_all(".")
    b.send_all(".")  # already sent: nothing more

    assert [cmd for cmd, _ in cast.sends()] == [
        ["cast", "send", "--async", "--nonce", "5", "--create", "0xaa"],
        ["cast", "send", "--async", "--nonce", "6", "--create", "0xbb"],
    ]
    assert (b.tx_hash(a1), b.tx_hash(a2)) == ("0xtx5", "0xtx6")


def test_send_all_passes_password_before_create(monkeypatch, cast):
    monkeypatch.setattr(broadcast, "cast_password", lambda: (["--password-file", "/dev/stdin"], 7))
    b = broadcast.Broadcast()
    b.create("00", ".")
    b.send_all(".")

    address_cmd, address_fd = cast.calls[0]
    assert address_cmd == ["cast", "wallet", "address", "--password-file", "/dev/stdin"]
    assert address_fd == 7
    assert cast.sends() == [
        (["cast", "send", "--async", "--password-file", "/dev/stdin", "--nonce", "5", "--create", "0x00"], 7)
    ]


def test_create_uses_eth_from_as_sender(monkeypatch, cast):
    monkeypatch.setenv("ETH_FROM", SENDER.lower())
    address, sender = broadcast.Broadcast().create("00", ".")

    assert sender == SENDER
    assert address == broadcast.create_address(SENDER, 5)
    assert not any(cmd[1] == "wallet" for cmd, _ in cast.calls)


def test_send_all_stops_at_a_rejected_send(monkeypatch, cast):
    b = broadcast.Broadcast()
    a1, _ = b.create("aa", ".")
    a2, _ = b.create("bb", ".")

    def reject_second(cmd, root=None, stdin=None, stdin_fd=None):
        if cmd[1] == "send" and "6" in cmd:
            raise click.ClickException("cast send failed")
        return cast(cmd, root, stdin, stdin_fd)

    monkeypatch.setattr(broadcast, "run", reject_second)
    with pytest.raises(click.ClickException):
        b.send_all(".")
    assert (b.tx_hash(a1), b.tx_hash(a2)) == ("0xtx5", None)


# -- wait ------------------------------------------------------------------------------


def _sent(*initcodes):
    b = broadcast.Broadcast()
    addresses = [b.create(code, ".")[0] for code in initcodes]
    b.send_all(".")
    return b, addresses


def test_wait_reports_each_confirmation(cast, capsys):
    b, (a1, a2) = _sent("aa", "bb")
    capsys.readouterr()

    b.wait(".")

    out = capsys.readouterr().out
    assert "waiting for 2 deployment transaction(s)" in out
    assert f"[1/2] {a1} confirmed in block 105" in out
    assert f"[2/2] {a2} confirmed in block 106" in out


def test_wait_ignores_unsent_creations(cast, capsys):
    b = broadcast.Broadcast()
    b.create("aa", ".")
    b.wait(".")
    assert not any(cmd[1] == "receipt" for cmd, _ in cast.calls)
    assert capsys.readouterr().out == ""


def test_wait_fails_on_revert_after_waiting_for_all(cast, capsys):
    cast.receipts["0xtx5"] = {"status": "0x0"}
    b, _ = _sent("aa", "bb")

    with pytest.raises(click.ClickException, match="tx 0xtx5 reverted"):
        b.wait(".")
    assert [cmd[2] for cmd, _ in cast.calls if cmd[1] == "receipt"] == ["0xtx5", "0xtx6"]
    assert "[1/2] tx 0xtx5 reverted" in capsys.readouterr().err


def test_wait_fails_when_contract_lands_elsewhere(cast):
    cast.receipts["0xtx5"] = {"contractAddress": "0x" + "ee" * 20}
    b, _ = _sent("aa")

    with pytest.raises(click.ClickException, match="expected"):
        b.wait(".")


# -- broadcast_session -------------------------------------------------------------------


def test_create_requires_session():
    with pytest.raises(RuntimeError, match="outside broadcast_session"):
        broadcast.create("00", ".")


def test_broadcast_session_is_not_reentrant():
    with broadcast.broadcast_session():
        with pytest.raises(RuntimeError, match="already active"):
            with broadcast.broadcast_session():
                pass
        assert broadcast._session is not None
    assert broadcast._session is None
