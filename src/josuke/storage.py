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


def _proxy_reads(steps: list[dict], proxy: int) -> dict[str, str]:
    """Each slot of `proxy`'s storage read in one traced call to it, in order, with its value.

    A frame runs in its callee's storage after CALL and STATICCALL, but in its
    caller's after DELEGATECALL and CALLCODE, so the reads of a dispatcher the proxy
    delegates to count, as do its delegate's."""
    reads = {}
    frames = [proxy]  # whose storage each depth uses; None for a created contract
    callee = None
    for step, after in zip(steps, steps[1:]):
        depth = step["depth"]
        if depth > len(frames):
            frames.append(callee)
        else:
            del frames[depth:]
        op, stack = step["opName"], step["stack"]
        if op == "SLOAD" and frames[-1] == proxy and after["depth"] == depth:
            key, value = int(stack[-1], 16), int(after["stack"][-1], 16)
            reads.setdefault(f"0x{key:064x}", f"0x{value:064x}")
        if op in ("CALL", "STATICCALL"):
            callee = int(stack[-2], 16) & _ADDRESS_MASK
        elif op in ("DELEGATECALL", "CALLCODE"):
            callee = frames[-1]
        elif op in ("CREATE", "CREATE2"):
            callee = None
    return reads


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
        EIP-3155 trace, then calling it again with each slot of its storage read, in
        turn, pointed at a stub delegate. The first slot that routes the call to the
        stub is the selector's, unless it routes another selector too, as an
        implementation slot holding a dispatcher does. A lone selector is probed
        beside a dummy to expose such a slot. Calling the method directly works even
        if `implementation` is not installed. Fails if no slot routes a selector."""
        wanted = list(dict.fromkeys(selector.selector for selector in selectors))
        dummies = [d for d in ("0xffffffff", "0xfffffffe") if d not in wanted]
        probed = wanted + dummies[: max(0, 2 - len(wanted))]
        proxy = self.address.lower()
        cache = {} if self.cache is None else self.cache
        trace = []
        with EvmRelay(cache=cache, on_trace=trace.append) as relay:
            for selector in probed:
                relay.call({"to": proxy, "data": selector})
        candidates = {
            selector: iter(_proxy_reads(steps, int(proxy, 16)).items())
            for selector, steps in zip(probed, _split_calls(trace), strict=True)
        }
        stub = {_STUB: {"code": delegate_stub(_STUB)}}
        routed = f"0x{_STUB[2:].rjust(64, '0')}"
        restore = {}  # the slot the last attempt pointed at the stub, as it was
        shared = set()  # slots that route more than one selector
        claims = {}  # slot -> the selector it routes
        found = {}  # selector -> (slot, value)
        queue = list(probed)
        with EvmRelay(cache=cache, json_output=True) as relay:

            def routes(selector: str, key: str, value: str) -> bool:
                nonlocal restore
                overrides = {**stub, proxy: {"stateDiff": {**restore, key: routed}}}
                result = json.loads(relay.call({"to": proxy, "data": selector, "stateOverrides": overrides}))
                restore = {key: value}
                return int(result["status"], 16) == 1 and result["returnData"] == routed

            while queue:
                selector = queue.pop()
                # Resumes where this selector left off, should a later one share its slot.
                for key, value in candidates[selector]:
                    if key in shared or not routes(selector, key, value):
                        continue
                    other = claims.pop(key, None)
                    if other is not None:
                        shared.add(key)
                        del found[other]
                        queue.append(other)
                        continue
                    claims[key] = selector
                    found[selector] = (key, value)
                    break
        for selector in wanted:
            if selector not in found:
                raise click.ClickException(f"{self.address}: no storage read routes {selector} to a delegate")
            self.storage_keys[selector], self.storage_values[selector] = found[selector]
