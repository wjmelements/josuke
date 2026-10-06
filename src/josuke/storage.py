import json

import click
from eth_utils import to_checksum_address

from .evm import EvmRelay
from .opcodes import MSIZE, MSTORE, PUSH0, PUSH20, RETURN
from .selectors import Selector

_ADDRESS_MASK = (1 << 160) - 1
# Stands in for a delegate while slot detection tries each slot the proxy reads.
_STUB = "0x" + "5b" * 20


def slot_address(raw: str | None) -> str | None:
    """The delegate address held in a 32-byte storage word, or None if empty."""
    if not raw or int(raw, 16) == 0:
        return None
    return to_checksum_address("0x" + raw.removeprefix("0x").rjust(64, "0")[-40:])


def delegate_stub(address: str) -> str:
    """Runtime that returns `address` as a word. Standing in for a delegate, it keeps
    routing checks about routing: it runs no facet code and reads no proxy storage.
    (`ADDRESS` would be the proxy's under `DELEGATECALL`, so it is a constant.)"""
    return f"0x{PUSH20}{address.removeprefix('0x').lower()}{PUSH0}{MSTORE}{MSIZE}{PUSH0}{RETURN}"


def _proxy_reads(steps: list[dict], proxy: int) -> list[str]:
    """Each slot of `proxy`'s storage read in one traced call to it, in order.

    A frame runs in its callee's storage after CALL and STATICCALL, but in its
    caller's after DELEGATECALL and CALLCODE, so the reads of a dispatcher the proxy
    delegates to count, as do its delegate's."""
    reads = {}
    frames = [proxy]  # whose storage each depth uses; None for a created contract
    callee = None
    for step in steps:
        depth = step["depth"]
        if depth > len(frames):
            frames.append(callee)
        else:
            del frames[depth:]
        op, stack = step["opName"], step["stack"]
        if op == "SLOAD" and frames[-1] == proxy:
            reads.setdefault(f"0x{int(stack[-1], 16):064x}")
        if op in ("CALL", "STATICCALL"):
            callee = int(stack[-2], 16) & _ADDRESS_MASK
        elif op in ("DELEGATECALL", "CALLCODE"):
            callee = frames[-1]
        elif op in ("CREATE", "CREATE2"):
            callee = None
    return list(reads)


class ProxyStorage:
    def __init__(self, address, cache: dict | None = None):
        self.address = address
        self.cache = cache  # shared RPC cache for the relay; see EvmRelay
        self.storage_keys = {} # Selector.selector -> storage_key
        self.storage_values = {} # Selector.selector -> storage_value

    def fetch(self, selectors: list[Selector]):
        """Find the slot routing each selector by calling the proxy with it under an
        EIP-3155 trace, then calling it again with each slot of its storage read, in
        turn, pointed at a stub delegate. The first slot that routes the call to the
        stub is the selector's, unless it routes another selector too, as an
        implementation slot holding a dispatcher does. A lone selector is probed
        beside a dummy to expose such a slot. Calling the method directly works even
        if `implementation` is not installed. Fails if no slot routes a selector.

        Every call replaces the proxy's storage with the slots read so far, so a slot
        is fetched only once a trace reads it, and the slots every selector newly
        reads are fetched in one batch, rather than one round trip each."""
        wanted = list(dict.fromkeys(selector.selector for selector in selectors))
        dummies = [d for d in ("0xffffffff", "0xfffffffe") if d not in wanted]
        probed = wanted + dummies[: max(0, 2 - len(wanted))]
        proxy = self.address.lower()
        # A whole account, so evm fetches none of it.
        stub = {_STUB: {"code": delegate_stub(_STUB), "nonce": "0x0", "balance": "0x0"}}
        routed = f"0x{_STUB[2:].rjust(64, '0')}"
        known = {}  # the proxy's storage, as read so far
        trace = []
        with EvmRelay(cache=self.cache, json_output=True, on_trace=trace.append) as relay:

            def call(selector: str, storage: dict) -> dict:
                trace.clear()
                overrides = {**stub, proxy: {"state": storage}}
                return json.loads(relay.call({"to": proxy, "data": selector, "stateOverrides": overrides}))

            def reads(selector: str) -> list[str]:
                call(selector, known)
                return _proxy_reads([step for step in trace if "opName" in step], int(proxy, 16))

            def routes(selector: str, key: str) -> bool:
                result = call(selector, {**known, key: routed})
                return int(result["status"], 16) == 1 and result["returnData"] == routed

            # Retraces the selectors that read a slot not yet fetched, until none do.
            candidates = {}
            pending = probed
            while pending:
                candidates.update((selector, reads(selector)) for selector in pending)
                missing = {key for selector in pending for key in candidates[selector]} - known.keys()
                if missing:
                    known.update(relay.storage_at(proxy, sorted(missing)))
                pending = [selector for selector in pending if not missing.isdisjoint(candidates[selector])]
            candidates = {selector: iter(read) for selector, read in candidates.items()}
            shared = set()  # slots that route more than one selector
            claims = {}  # slot -> the selector it routes
            found = {}  # selector -> slot
            queue = list(probed)
            while queue:
                selector = queue.pop()
                # Resumes where this selector left off, should a later one share its slot.
                for key in candidates[selector]:
                    if key in shared or not routes(selector, key):
                        continue
                    other = claims.pop(key, None)
                    if other is not None:
                        shared.add(key)
                        del found[other]
                        queue.append(other)
                        continue
                    claims[key] = selector
                    found[selector] = key
                    break
        for selector in wanted:
            if selector not in found:
                raise click.ClickException(f"{self.address}: no storage read routes {selector} to a delegate")
            self.storage_keys[selector] = found[selector]
            self.storage_values[selector] = known[found[selector]]
