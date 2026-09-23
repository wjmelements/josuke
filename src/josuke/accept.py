import pathlib
from os import environ

import click
from eth_utils import to_checksum_address

from .ethjsonrpc import chain_id
from .ledger import load_ledger, write_ledger
from .storage import ProxyStorage, slot_address as _slot_address
from .verify import Report, _selector_owners
from .worktree import SourceTrees


def verify_migrated(
    current: dict | None,
    proposed: dict,
    storage: ProxyStorage,
    current_tree: pathlib.Path | None,
    proposed_tree: pathlib.Path,
    report: Report,
) -> None:
    """The proxy's live dispatch state matches `proposed`.

    Every selector owned by a proposed facet — newly deployed or carried over
    unchanged — routes to that facet's recorded address, and every selector that
    `proposed` drops relative to `current` is cleared. This is what the migration
    script was built to do, so a clean result means it ran.
    """
    proposed_owners, _ = _selector_owners(proposed, proposed_tree)
    current_owners = {}
    if current:
        current_owners, _ = _selector_owners(current, current_tree)

    for selector, (source_id, address) in sorted(proposed_owners.items()):
        routed = _slot_address(storage.storage_values.get(selector))
        if address is None:
            report.fail(f"accept {selector} ({source_id}): proposed facet has no recorded address")
            continue
        if routed != to_checksum_address(address):
            report.fail(
                f"accept {selector} ({source_id}): routes to {routed}, expected {address} "
                f"(migration not run?)"
            )

    for selector in sorted(current_owners.keys() - proposed_owners.keys()):
        routed = _slot_address(storage.storage_values.get(selector))
        if routed is not None:
            source_id = current_owners[selector][0]
            report.fail(
                f"accept {selector} ({source_id}): still routes to {routed}, expected removal"
            )


def accepted_state(current: dict, proposed: dict) -> dict:
    """The deployment state that replaces `current` once `proposed`'s migration is confirmed.

    `proposed.facets` is already the complete resolved facet set — `deploy`
    copies unchanged facets forward — so acceptance is a wholesale swap and
    `current`'s prior contents are dropped. Both states are passed in because a
    planned schema change will fold the outgoing facets into a `previous` record;
    that belongs here.
    """
    accepted = {"gitCommit": proposed["gitCommit"], "facets": proposed["facets"]}
    if "selectors" in proposed:
        accepted["selectors"] = proposed["selectors"]
    if "migration" in proposed:
        accepted["migration"] = proposed["migration"]
    return accepted


def run_accept(ledger_path):
    if "ETH_RPC_URL" not in environ:
        raise click.ClickException("ETH_RPC_URL is not set")

    root = pathlib.Path.cwd()
    ledger = load_ledger(ledger_path)
    chain = chain_id()
    report = Report()

    pending = []  # (proxy, history, accepted_state)
    with SourceTrees(root) as trees:
        for entry in ledger:
            report.proxy = proxy = to_checksum_address(entry["address"])
            history = entry.get("deployments", {}).get(chain)
            proposed = history.get("proposed") if history else None
            if not proposed:
                click.echo(f"{proxy}: no proposed deployment on chain {chain}")
                continue
            current = history.get("current")

            proposed_tree = trees.get(proposed["gitCommit"])
            current_tree = trees.get(current["gitCommit"]) if current else None

            _, proposed_sels = _selector_owners(proposed, proposed_tree)
            selectors = {s.selector: s for s in proposed_sels}
            if current:
                _, current_sels = _selector_owners(current, current_tree)
                selectors.update((s.selector, s) for s in current_sels)

            storage = ProxyStorage(proxy)
            if selectors:
                storage.fetch(list(selectors.values()))

            before = len(report.failures)
            verify_migrated(current, proposed, storage, current_tree, proposed_tree, report)
            if len(report.failures) > before:
                continue  # leave this proxy's proposed state untouched

            pending.append((proxy, history, accepted_state(current or {}, proposed)))

    if report.failures:
        raise click.ClickException(
            f"{len(report.failures)} acceptance check failure(s):\n"
            + "\n".join(f"  - {failure}" for failure in report.failures)
        )

    if not pending:
        click.echo("nothing to accept")
        return

    for proxy, history, accepted in pending:
        history["current"] = accepted
        del history["proposed"]
        click.echo(f"{proxy} chain {chain}: accepted proposed -> current")
    write_ledger(ledger_path, ledger)
