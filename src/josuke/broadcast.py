"""Plan every contract creation first, then send them all back to back and wait for them together.

A CREATE address depends only on the sender and nonce, so `create` reserves the
next nonce and returns the address without sending anything; planning (building
the migration from those addresses, and so on) finishes before the first
transaction goes out. `send_all` then sends each with `cast send --async` and its
explicit `--nonce`, with nothing slow in between, so they can land in the same
block. `wait` collects every receipt and checks each contract landed where planned.
"""

import json
import pathlib
from contextlib import contextmanager
from dataclasses import dataclass
from os import environ

import click
import click_spinner
from eth_utils import keccak, to_checksum_address

from .ethjsonrpc import rpc_batch
from .proc import run
from .signer import cast_password

_session: "Broadcast | None" = None


def _rlp_uint(n: int) -> bytes:
    if n == 0:
        return b"\x80"
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return raw if len(raw) == 1 and raw[0] < 0x80 else bytes([0x80 + len(raw)]) + raw


def create_address(sender: str, nonce: int) -> str:
    """The address a CREATE from `sender` at `nonce` deploys to: keccak(rlp([sender, nonce]))[12:]."""
    payload = b"\x94" + bytes.fromhex(sender.removeprefix("0x")) + _rlp_uint(nonce)
    return to_checksum_address(keccak(bytes([0xC0 + len(payload)]) + payload)[12:])


@dataclass
class Creation:
    nonce: int
    initcode_hex: str
    address: str
    tx_hash: str | None = None  # set once sent


class Broadcast:
    """Contract creations planned in this session, sent by `send_all`, mined by `wait`."""

    def __init__(self, chain_id: str):
        self.chain_id = chain_id  # decimal, passed to `cast` so it need not look it up
        self.sender: str | None = None
        self.next_nonce: int | None = None
        self.creations: list[Creation] = []

    def _start(self, root: pathlib.Path) -> None:
        if environ.get("ETH_FROM"):
            self.sender = to_checksum_address(environ["ETH_FROM"])
        else:
            password_args, password_fd = cast_password()
            address = run(["cast", "wallet", "address", *password_args], root, stdin_fd=password_fd)
            self.sender = to_checksum_address(address.strip())
        self.next_nonce = int(run(["cast", "nonce", self.sender, "--block", "pending"], root).strip())

    def create(self, initcode_hex: str, root: pathlib.Path) -> tuple[str, str]:
        """Reserve the next nonce for a contract creation; (address it will deploy to, sender)."""
        if self.sender is None:
            self._start(root)
        creation = Creation(self.next_nonce, initcode_hex, create_address(self.sender, self.next_nonce))
        self.next_nonce += 1
        self.creations.append(creation)
        return creation.address, self.sender

    def tx_hash(self, address: str) -> str | None:
        return next((c.tx_hash for c in self.creations if c.address == address), None)

    def _gas(self, unsent: list[Creation]) -> tuple[list[str], list[int]]:
        """(fee flags shared by every send, gas limit per creation), from one batched
        request, so each `cast send` needs no lookups of its own. A creation whose
        constructor reverts fails here, before anything is sent."""
        block, priority, *limits = rpc_batch(
            [
                ("eth_getBlockByNumber", ["latest", False]),
                ("eth_maxPriorityFeePerGas", []),
                *(("eth_estimateGas", [{"from": self.sender, "data": "0x" + c.initcode_hex}]) for c in unsent),
            ]
        )
        flags = []
        if block.get("baseFeePerGas") is not None:  # EIP-1559; otherwise `cast` prices it
            priority = int(priority, 16)
            max_fee = 2 * int(block["baseFeePerGas"], 16) + priority  # as `cast` computes it
            flags = ["--gas-price", str(max_fee), "--priority-gas-price", str(priority)]
        return flags, [int(limit, 16) for limit in limits]

    def send_all(self, root: pathlib.Path) -> None:
        """Send every planned creation, in nonce order, without waiting for any to be mined."""
        unsent = [c for c in self.creations if c.tx_hash is None]
        if not unsent:
            return
        fee_flags, limits = self._gas(unsent)
        for creation, limit in zip(unsent, limits):
            password_args, password_fd = cast_password()
            creation.tx_hash = run(
                [
                    "cast", "send", "--async", *password_args, "--chain", self.chain_id, *fee_flags,
                    "--gas-limit", str(limit), "--nonce", str(creation.nonce),
                    "--create", "0x" + creation.initcode_hex,
                ],
                root,
                stdin_fd=password_fd,
            ).strip()
            click.echo(f"  tx {creation.tx_hash} (nonce {creation.nonce}) -> {creation.address}")

    def wait(self, root: pathlib.Path) -> None:
        """Block until every sent creation is mined, reporting each as it confirms; fail
        unless each deployed where planned."""
        sent = [c for c in self.creations if c.tx_hash is not None]
        if not sent:
            return
        total = len(sent)
        click.echo(f"waiting for {total} deployment transaction(s)...")
        failures = []
        for i, creation in enumerate(sent, 1):
            with click_spinner.spinner():
                receipt = json.loads(run(["cast", "receipt", creation.tx_hash, "--json"], root))
            block = int(receipt.get("blockNumber") or "0x0", 16)
            if int(receipt.get("status") or "0x0", 16) != 1:
                failure = f"tx {creation.tx_hash} reverted"
            elif to_checksum_address(receipt.get("contractAddress") or "0x" + "00" * 20) != creation.address:
                failure = f"tx {creation.tx_hash} deployed to {receipt.get('contractAddress')}, expected {creation.address}"
            else:
                click.echo(f"  [{i}/{total}] {creation.address} confirmed in block {block}")
                continue
            failures.append(failure)
            click.echo(f"  [{i}/{total}] {failure} (block {block})", err=True)
        if failures:
            raise click.ClickException("deployment failed:\n" + "\n".join(f"  - {f}" for f in failures))


@contextmanager
def broadcast_session(chain_id: str):
    """Scope for `create` on `chain_id`; not reentrant, since one session owns the nonce sequence."""
    global _session
    if _session is not None:
        raise RuntimeError("broadcast_session is already active")
    _session = Broadcast(chain_id)
    try:
        yield _session
    finally:
        _session = None


def create(initcode_hex: str, root: pathlib.Path) -> tuple[str, str]:
    if _session is None:
        raise RuntimeError("create outside broadcast_session")
    return _session.create(initcode_hex, root)
