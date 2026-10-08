import json

from .ethjsonrpc import eth_get_code
from .evm import EvmRelay
from .storage import delegate_stub, slot_address


def rehearse_migration(
    proxy: str, migration_runtime: bytes, routes: dict, delegates, cache: dict | None = None
) -> list[str]:
    """Failures from running `migration_runtime` as `proxy`'s code against its live
    storage, then asking the proxy's real dispatcher where each selector goes.

    `routes` maps each selector to the address it must reach afterwards, or to None
    when it must revert. Every address in `delegates` is served as a stub returning
    that address, so a wrong slot shows up as the wrong address or a revert,
    whichever slot slot detection guessed."""
    proxy = proxy.lower()
    stubs = {address.lower(): {"code": delegate_stub(address)} for address in delegates}
    failures = []
    with EvmRelay(cache=cache, json_output=True) as relay:

        def call(data: str, overrides: dict) -> tuple[bool, str]:
            request = {"to": proxy, "data": data, "stateOverrides": overrides}
            result = json.loads(relay.call(request))
            return int(result["status"], 16) == 1, result["returnData"].removeprefix("0x")

        # State overrides persist, so the migration's writes stay in the proxy's
        # storage after its real code is put back for the dispatch queries.
        ok, data = call("0x", {**stubs, proxy: {"code": "0x" + migration_runtime.hex()}})
        if not ok:
            return [f"migration reverted{f' with 0x{data}' if data else ''}"]
        restore = {proxy: {"code": eth_get_code(proxy)}}
        for selector, want in sorted(routes.items()):
            ok, data = call(selector, restore)
            restore = {}
            got = (len(data) == 64 and slot_address(data)) or f"0x{data}"
            if want is None:
                if ok:
                    failures.append(f"{selector} routes to {got}, expected removal")
            elif not ok:
                failures.append(f"{selector} reverts, expected {want}")
            elif got.lower() != want.lower():
                failures.append(f"{selector} routes to {got}, expected {want}")
    return failures
