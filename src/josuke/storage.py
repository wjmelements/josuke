from dataclasses import dataclass

import click
from eth_utils import keccak, to_checksum_address

from .evm import EvmRelay
from .selectors import Selector

_ADDRESS_MASK = (1 << 160) - 1
# A route kept in a struct may sit a few words past its mapping slot.
_STRUCT_REACH = 256


def slot_address(raw: str | None) -> str | None:
    """The delegate address held in a 32-byte storage word, or None if empty."""
    if not raw or int(raw, 16) == 0:
        return None
    return to_checksum_address("0x" + raw.removeprefix("0x").rjust(64, "0")[-40:])


@dataclass
class _Read:
    """One SLOAD of the proxy's storage during a probe call."""

    key: int
    value: int | None  # None when the trace ends before showing it
    keyed_by_selector: bool  # the selector itself, or a keccak of something containing it
    delegated: bool  # the proxy then DELEGATECALLed the address it read
    inspected: bool  # the proxy then checked the code at the address it read


def _proxy_reads(steps: list[dict], proxy: int, selector: str) -> list[_Read]:
    """The reads of `proxy`'s storage in one traced call to it, in order.

    A frame runs in its callee's storage after CALL and STATICCALL, but in its
    caller's after DELEGATECALL and CALLCODE, so a delegate's reads of proxy
    storage count too, as do the reads of a dispatcher the proxy delegates to."""
    sig = bytes.fromhex(selector.removeprefix("0x"))
    derived = {int.from_bytes(sig), int.from_bytes(sig) << 224}  # right- and left-aligned
    hashes = set()  # keccaks whose preimage holds the selector, or such a keccak
    loads = []  # (key, value)
    targets = set()
    inspections = set()  # EXTCODESIZE and EXTCODEHASH operands, as before a call
    frames = [proxy]  # whose storage each depth uses; None for a created contract
    callee = None
    for i, step in enumerate(steps):
        depth = step["depth"]
        if depth > len(frames):
            frames.append(callee)
        else:
            del frames[depth:]
        storage = frames[-1]
        op, stack = step["opName"], step["stack"]
        if op in ("SHA3", "KECCAK256"):
            offset, size = int(stack[-1], 16), int(stack[-2], 16)
            memory = bytes.fromhex(step["memory"].removeprefix("0x"))
            preimage = memory[offset : offset + size].ljust(size, b"\0")
            if sig in preimage or any(h.to_bytes(32) in preimage for h in hashes):
                hashes.add(int.from_bytes(keccak(preimage)))
        elif op == "SLOAD" and storage == proxy:
            after = steps[i + 1] if i + 1 < len(steps) else None
            value = int(after["stack"][-1], 16) if after and after["depth"] == depth else None
            loads.append((int(stack[-1], 16), value))
        elif op == "DELEGATECALL" and storage == proxy:
            targets.add(int(stack[-2], 16) & _ADDRESS_MASK)
        elif op in ("EXTCODESIZE", "EXTCODEHASH") and storage == proxy:
            inspections.add(int(stack[-1], 16) & _ADDRESS_MASK)
        if op in ("CALL", "STATICCALL"):
            callee = int(stack[-2], 16) & _ADDRESS_MASK
        elif op in ("DELEGATECALL", "CALLCODE"):
            callee = storage
        elif op in ("CREATE", "CREATE2"):
            callee = None
    return [
        _Read(
            key,
            value,
            key in derived or any(0 <= key - h < _STRUCT_REACH for h in hashes),
            bool(value) and value & _ADDRESS_MASK in targets,
            bool(value) and value & _ADDRESS_MASK in inspections,
        )
        for key, value in loads
    ]


def _split_calls(trace: list[dict]) -> list[list[dict]]:
    """The steps of each traced call; evm ends each call's steps with a summary line."""
    calls, steps = [], []
    for line in trace:
        if "opName" in line:
            steps.append(line)
        else:
            calls.append(steps)
            steps = []
    return calls


class ProxyStorage:
    def __init__(self, address, cache: dict | None = None):
        self.address = address
        self.cache = cache  # shared RPC cache for the relay; see EvmRelay
        self.storage_keys = {} # Selector.selector -> storage_key
        self.storage_values = {} # Selector.selector -> storage_value

    def fetch(self, selectors: list[Selector]):
        """Find the slot routing each selector by calling the proxy with it under an
        EIP-3155 trace. Calling the method directly works even if `implementation` is
        not installed.

        Of the proxy's storage reads during each call, the dispatch read is taken to be
        the first that best fits, in order: a key derived from the selector (a solidity
        mapping, or a keccak of the selector with a namespace); a value the proxy then
        delegates to; a value whose code the proxy then checks, as solidity does before
        a call, even when that check reverts; a key no other selector reads. A proxy migrated from ERC-1822 that
        reads its old implementation slot first, or a delegate reading proxy storage,
        fits none of them. A selector with no read that fits fails. The rehearsal checks
        the result independently."""
        selectors = list(selectors)
        proxy_address = self.address.lower()
        trace = []
        with EvmRelay(cache=self.cache, on_trace=trace.append, trace_memory=True) as relay:
            for selector in selectors:
                relay.call({"to": proxy_address, "data": selector.selector})
        proxy = int(proxy_address, 16)
        reads = {
            selector.selector: _proxy_reads(steps, proxy, selector.selector)
            for selector, steps in zip(selectors, _split_calls(trace))
        }
        readers = {}
        for selector, selector_reads in reads.items():
            for read in selector_reads:
                readers.setdefault(read.key, set()).add(selector)
        for selector, selector_reads in reads.items():
            fits = [
                r
                for r in selector_reads
                if r.keyed_by_selector or r.delegated or r.inspected or len(readers[r.key]) == 1
            ]
            if not fits:
                raise click.ClickException(f"{self.address}: no storage read looks like the dispatch for {selector}")
            read = min(fits, key=lambda r: (not r.keyed_by_selector, not r.delegated, not r.inspected))
            self.storage_keys[selector] = f"0x{read.key:064x}"
            if read.value is not None:
                self.storage_values[selector] = f"0x{read.value:064x}"
