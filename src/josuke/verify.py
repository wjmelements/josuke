import pathlib
from os import environ

import click
from eth_utils import to_checksum_address

from .deploy import (
    build_migration,
    facet_from_source_id,
    facet_initcode,
    facet_selectors,
    keccak_hex,
    resolve_facets,
    selectors_runtime,
)
from .erc8167 import SELECTORS_SELECTOR
from .ethjsonrpc import chain_id, eth_get_code
from .evm import EvmRelay
from .ledger import load_ledger
from .migration import InvalidMigration, Migration
from .selectors import Selector
from .storage import ProxyStorage, slot_address as _slot_address
from .summary import summarize_upgrade
from .worktree import SourceTrees

ZERO_ADDRESS = to_checksum_address("0x" + "00" * 20)


class Report:
    """Collects verification failures, each tagged with the proxy under test."""

    def __init__(self):
        self.failures: list[str] = []
        self.proxy = ""

    def fail(self, message: str) -> None:
        self.failures.append(f"{self.proxy} {message}")


def _onchain_codehash(address: str) -> str:
    return keccak_hex(eth_get_code(address))


def _replay_runtime(initcode: str, sender: str | None, cache: dict | None) -> str:
    """The runtime bytecode `initcode`'s constructor leaves on chain, replayed
    against live state (as `sender`, when an immutable is derived from it)."""
    request = {"data": initcode}
    if sender:
        request["from"] = sender
    with EvmRelay(cache=cache) as relay:
        return relay.call(request)


def verify_facets(
    state: dict, label: str, tree: pathlib.Path, report: Report, rpc_cache: dict | None = None
) -> None:
    """Each recorded facet is built from `state`'s commit and live on chain."""
    for source_id, rec in state["facets"].items():
        facet = facet_from_source_id(source_id)
        initcode, _ = facet_initcode(facet, tree, rec.get("constructorArgs"), prompt=False)
        got = keccak_hex(initcode)
        if got != rec["initcodeHash"]:
            report.fail(f"{label} {source_id}: initcodeHash {got} != recorded {rec['initcodeHash']}")

        # Rebuild the runtime from source and hash that too. The check above only
        # ties the initcode to source; without this, a codehash recorded to match
        # tampered on-chain code would pass.
        got = keccak_hex(_replay_runtime(initcode, rec.get("from"), rpc_cache))
        if got != rec["codehash"]:
            report.fail(f"{label} {source_id}: codehash {got} rebuilt from source != recorded {rec['codehash']}")

        address = rec.get("address")
        if address is None:
            report.fail(f"{label} {source_id}: no recorded address; cannot check deployed code")
            continue
        got = _onchain_codehash(address)
        if got != rec["codehash"]:
            report.fail(f"{label} {source_id} @{address}: codehash {got} != recorded {rec['codehash']}")


def _selector_owners(state: dict, tree: pathlib.Path) -> tuple[dict, list]:
    """(selector -> (source_id, address), [Selector]) for a deployment state.

    Includes the generated `selectors()` delegate recorded under `state.selectors`,
    so dispatch and acceptance checks cover it like any facet selector."""
    owners, selectors = {}, []
    for source_id, rec in state["facets"].items():
        for selector in facet_selectors(facet_from_source_id(source_id), tree):
            owners[selector.selector] = (source_id, rec.get("address"))
            selectors.append(selector)
    impl = state.get("selectors")
    if impl and SELECTORS_SELECTOR not in owners:
        owners[SELECTORS_SELECTOR] = ("selectors()", impl.get("address"))
        selectors.append(Selector(SELECTORS_SELECTOR, "selectors()"))
    return owners, selectors


def verify_dispatch(current: dict, storage: ProxyStorage, tree: pathlib.Path, report: Report) -> None:
    """The proxy routes every `current` selector to its recorded facet address."""
    owners, _ = _selector_owners(current, tree)
    for selector, (source_id, address) in sorted(owners.items()):
        routed = _slot_address(storage.storage_values.get(selector))
        if address is None:
            if routed is not None:
                report.fail(f"current dispatch {selector} ({source_id}): routes to {routed}, nothing recorded")
            continue
        if routed != to_checksum_address(address):
            report.fail(f"current dispatch {selector} ({source_id}): routes to {routed}, expected {address}")


def verify_selectors(state: dict, label: str, tree: pathlib.Path, report: Report) -> None:
    """The recorded `selectors()` delegate holds the method generated for this facet set.

    Routing to that address is covered by `verify_dispatch` / `verify_migration`
    via `_selector_owners`; this checks the deployed bytecode itself."""
    impl = state.get("selectors")
    if not impl:
        return

    facets = [facet_from_source_id(source_id) for source_id in state["facets"]]
    runtime = selectors_runtime(facets, tree)
    address = impl.get("address")
    if runtime is None:
        report.fail(f"{label}.selectors @{address}: a facet implements selectors(); it should not be recorded")
        return
    if address is None:
        report.fail(f"{label}.selectors: no recorded address")
        return

    onchain = bytes.fromhex(eth_get_code(address).removeprefix("0x"))
    if onchain != runtime:
        report.fail(
            f"{label}.selectors @{address}: on-chain code is not the generated selectors() "
            f"for this facet set"
        )


def verify_proposed_set(facet_src: list, proposed: dict, tree: pathlib.Path, report: Report) -> None:
    """`proposed.facets` is exactly what `facetSrc` resolves to at its commit."""
    resolved = {facet.source_id for facet in resolve_facets(facet_src, tree)}
    recorded = set(proposed["facets"])
    for source_id in sorted(resolved - recorded):
        report.fail(f"proposed: {source_id} resolves from facetSrc but is missing from proposed.facets")
    for source_id in sorted(recorded - resolved):
        report.fail(f"proposed: proposed.facets has {source_id}, not matched by facetSrc")


def verify_migration(
    proxy: str,
    proposed: dict,
    current: dict,
    storage: ProxyStorage,
    current_tree: pathlib.Path | None,
    tree: pathlib.Path,
    report: Report,
) -> None:
    """The on-chain migration installs every proposed selector and zeroes removals.

    `current_tree` checks out `current.gitCommit` (None when nothing is current)."""
    address = proposed["migration"]["address"]
    facets = [facet_from_source_id(source_id) for source_id in proposed["facets"]]
    expected = build_migration(
        proxy, facets, proposed["facets"], current, current_tree, tree,
        storage=storage, selectors_impl=proposed.get("selectors"),
    )
    if expected is None:
        report.fail(f"proposed.migration @{address}: recorded, but recomputation needs no migration")
        return

    onchain = bytes.fromhex(eth_get_code(address).removeprefix("0x"))
    try:
        got = {sd.selector: sd for sd in Migration.decode(onchain).setdelegates}
    except (InvalidMigration, KeyError):
        report.fail(f"proposed.migration @{address}: on-chain code is not a recognized migration script")
        return

    want = {sd.selector: sd for sd in expected.setdelegates}
    for selector, sd in sorted(want.items()):
        removal = sd.delegate.address == ZERO_ADDRESS
        action = "zero" if removal else f"install {sd.delegate.address} for"
        if selector not in got:
            report.fail(f"proposed.migration: does not {action} selector {selector}")
        elif got[selector].delegate.address != sd.delegate.address:
            report.fail(
                f"proposed.migration: selector {selector} -> {got[selector].delegate.address}, "
                f"expected {sd.delegate.address}"
            )
        elif got[selector].storage_key32 != sd.storage_key32:
            report.fail(f"proposed.migration: selector {selector} writes the wrong storage slot")
    for selector in sorted(got.keys() - want.keys()):
        report.fail(f"proposed.migration: unexpected entry for selector {selector}")


def run_verify(ledger_path):
    if "ETH_RPC_URL" not in environ:
        raise click.ClickException("ETH_RPC_URL is not set")

    root = pathlib.Path.cwd()
    ledger = load_ledger(ledger_path)
    chain = chain_id()
    report = Report()
    verified: list[str] = []
    rpc_cache: dict = {}  # shared across every facet replay for the run

    with SourceTrees(root) as trees:
        for entry in ledger:
            report.proxy = proxy = to_checksum_address(entry["address"])
            history = entry.get("deployments", {}).get(chain)
            if not history:
                click.echo(f"{proxy}: nothing recorded on chain {chain}")
                continue

            current = history.get("current")
            proposed = history.get("proposed")

            selectors = {}  # selector -> Selector, deduped across current + proposed
            if current:
                _, current_sels = _selector_owners(current, trees.get(current["gitCommit"]))
                selectors.update((s.selector, s) for s in current_sels)
            if proposed:
                _, proposed_sels = _selector_owners(proposed, trees.get(proposed["gitCommit"]))
                selectors.update((s.selector, s) for s in proposed_sels)

            storage = ProxyStorage(proxy)
            if selectors:
                storage.fetch(list(selectors.values()))

            if current:
                tree = trees.get(current["gitCommit"])
                before = len(report.failures)
                verify_facets(current, "current", tree, report, rpc_cache)
                verify_dispatch(current, storage, tree, report)
                verify_selectors(current, "current", tree, report)
                if len(report.failures) == before:
                    verified.append(proxy)
                    click.secho(f"✓ {proxy}: current deployment verified", fg="green")

            if proposed:
                tree = trees.get(proposed["gitCommit"])
                verify_facets(proposed, "proposed", tree, report, rpc_cache)
                verify_proposed_set(entry["facetSrc"], proposed, tree, report)
                verify_selectors(proposed, "proposed", tree, report)
                if "migration" in proposed:
                    current_tree = trees.get(current["gitCommit"]) if current else None
                    verify_migration(proxy, proposed, current or {}, storage, current_tree, tree, report)
                summarize_upgrade(
                    proxy, chain, current, proposed,
                    trees.get(current["gitCommit"]) if current else None,
                    tree, storage,
                )

    if report.failures:
        raise click.ClickException(
            f"{len(report.failures)} verification failure(s):\n"
            + "\n".join(f"  - {failure}" for failure in report.failures)
        )
    click.secho(
        f"✓ verified chain {chain}: {len(verified)}/{len(ledger)} "
        f"proxy entr{'y' if len(ledger) == 1 else 'ies'} confirmed current",
        fg="green", bold=True,
    )
