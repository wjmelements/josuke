"""Human-readable rendering of a proposed upgrade, for `josuke verify`.

`verify` runs the machine checks; this renders the same proposal in the terms a
proxy owner reviews before signing the migration: which facets are new, changed
or removed, their addresses and constructor arguments, and the selectors each
one serves — with selectors not in the current deployment called out first.
"""

import json
import pathlib

import click

from .deploy import facet_from_source_id, facet_selectors
from .erc8167 import SELECTORS_SELECTOR, selectors_selector
from .proc import run
from .storage import slot_address

# Width of the signature column before the `[0x…]` selector; longer signatures
# wrap the bracket to the next line.
SIG_COL = 44


def _commit_stamp(tree: pathlib.Path) -> str:
    """`<short-hash> <YYYY-MM-DD>` for a worktree's HEAD."""
    return run(["git", "-C", str(tree), "show", "-s", "--format=%h %cs", "HEAD"]).strip()


def _short(address: str | None) -> str:
    if not address:
        return "?"
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def _hash_short(digest: str | None) -> str:
    return f"{digest[:6]}…{digest[-4:]}" if digest else "?"


def _fmt(value) -> str:
    return json.dumps(value)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _facet_label(source_id: str) -> str:
    """A short name for cross-references ("moved to Emergency")."""
    if source_id == "selectors()":
        return "selectors()"
    facet = facet_from_source_id(source_id)
    return facet.contract or pathlib.Path(facet.path).name


def _selectors_by_facet(state: dict, tree) -> dict:
    """source_id -> [Selector], or source_id -> None when the facet won't build."""
    out = {}
    for source_id in state["facets"]:
        try:
            out[source_id] = facet_selectors(facet_from_source_id(source_id), tree)
        except click.ClickException:
            out[source_id] = None
    return out


def _owner_map(by_facet: dict, state: dict) -> dict:
    """selector4 -> source_id, including the generated `selectors()` delegate."""
    owner = {}
    for source_id, selectors in by_facet.items():
        for selector in selectors or []:
            owner.setdefault(selector.selector, source_id)
    if state.get("selectors") and SELECTORS_SELECTOR not in owner:
        owner[SELECTORS_SELECTOR] = "selectors()"
    return owner


def _sel_row(indent: str, prefix: str, selector, suffix: str = "") -> str:
    tag = f"  {suffix}" if suffix else ""
    left = f"{prefix}{selector.expressive}"
    bracket = f"[{selector.selector}]"
    if len(left) <= SIG_COL:
        return f"{indent}{left.ljust(SIG_COL)} {bracket}{tag}"
    return f"{indent}{left}\n{indent}{' ' * (SIG_COL + 1)}{bracket}{tag}"


def _constructor_block(args: dict, old: dict | None) -> None:
    click.echo("    constructor")
    for name, value in args.items():
        changed = old is not None and name in old and old[name] != value
        mark = "~ " if changed else "  "
        if isinstance(value, list) and len(value) > 1:
            click.echo(f"    {mark}{name}")
            for elem in value:
                click.echo(f"        {_fmt(elem)}")
            if changed:
                click.echo(f"        (was {_fmt(old[name])})")
        else:
            was = f"  (was {_fmt(old[name])})" if changed else ""
            click.echo(f"    {mark}{name.ljust(12)} {_fmt(value)}{was}")


def _facet_status(source_id: str, rec: dict, cur_facets: dict) -> str:
    if source_id not in cur_facets:
        return "NEW"
    if rec.get("initcodeHash") != cur_facets[source_id].get("initcodeHash"):
        return "CHANGED"
    return "UNCHANGED"


def _lost_list(selectors, moved: dict) -> None:
    for selector in sorted(selectors, key=lambda s: s.expressive):
        if selector.selector in moved:
            target = _facet_label(moved[selector.selector][1])
            click.echo(_sel_row("      ", "» ", selector, f"moved to {target}"))
        else:
            click.echo(_sel_row("      ", "- ", selector, "dropped"))


def summarize_upgrade(
    proxy: str,
    chain: str,
    current: dict | None,
    proposed: dict,
    current_tree,
    proposed_tree,
    storage,
) -> None:
    """Print, for a proxy with a pending upgrade, what `proposed` changes."""
    cur_facets = (current or {}).get("facets", {})
    prop_facets = proposed["facets"]

    cur_by_facet = _selectors_by_facet(current, current_tree) if current else {}
    prop_by_facet = _selectors_by_facet(proposed, proposed_tree)
    cur_owner = _owner_map(cur_by_facet, current or {})
    prop_owner = _owner_map(prop_by_facet, proposed)

    lookup = {}
    for group in (*cur_by_facet.values(), *prop_by_facet.values()):
        for selector in group or []:
            lookup[selector.selector] = selector
    if SELECTORS_SELECTOR in cur_owner or SELECTORS_SELECTOR in prop_owner:
        lookup.setdefault(SELECTORS_SELECTOR, selectors_selector())

    # Classify real facet methods only; the generated `selectors()` route is
    # infrastructure, described by its own line, not a "new selector".
    def _methods(owner):
        return {
            s: sid
            for s, sid in owner.items()
            if not (s == SELECTORS_SELECTOR and sid == "selectors()")
        }

    cur_methods, prop_methods = _methods(cur_owner), _methods(prop_owner)
    new = {s for s in prop_methods if s not in cur_methods}
    removed = {s for s in cur_methods if s not in prop_methods}
    moved = {
        s: (cur_methods[s], prop_methods[s])
        for s in prop_methods
        if s in cur_methods and cur_methods[s] != prop_methods[s]
    }

    # -- header --
    click.echo("")
    click.echo(f"{_short(proxy)}  chain {chain}  ·  proposed upgrade")
    if current:
        click.echo(f"  current   {_commit_stamp(current_tree)}")
    else:
        click.echo("  current   (none — first deployment)")
    click.echo(f"  proposed  {_commit_stamp(proposed_tree)}")

    # -- new selectors, up front --
    click.echo("")
    click.echo(f"NEW SELECTORS ({len(new)})  — not served by the current deployment")
    if not new:
        click.echo("  none")
    for selector4 in sorted(new, key=lambda s: lookup[s].expressive):
        click.echo(
            _sel_row("  ", "", lookup[selector4], f"→ {_facet_label(prop_owner[selector4])}")
        )

    # -- facets --
    click.echo("")
    click.echo("FACETS")

    for source_id, rec in prop_facets.items():
        status = _facet_status(source_id, rec, cur_facets)
        selectors = prop_by_facet.get(source_id)
        old = cur_facets.get(source_id, {})

        click.echo("")
        click.echo(f"  {status.ljust(9)} {_facet_label(source_id).ljust(20)} {source_id}")

        if selectors is None:
            click.echo("    (could not build — see checks below)")
            continue

        if status == "UNCHANGED":
            addr = rec.get("address") or old.get("address")
            click.echo(f"    {_short(addr)}  ·  {_plural(len(selectors), 'selector')}")
            continue

        addr, old_addr = rec.get("address"), old.get("address")
        if status == "CHANGED" and old_addr and old_addr != addr:
            click.echo(f"    address       {_short(addr)}  (was {_short(old_addr)})")
        else:
            click.echo(f"    address       {_short(addr)}")

        ih, old_ih = rec.get("initcodeHash"), old.get("initcodeHash")
        if status == "CHANGED" and old_ih and old_ih != ih:
            click.echo(f"    initcodeHash  {_hash_short(ih)}  (was {_hash_short(old_ih)})")
        else:
            click.echo(f"    initcodeHash  {_hash_short(ih)}")

        args = rec.get("constructorArgs")
        if args:
            _constructor_block(args, old.get("constructorArgs") if status == "CHANGED" else None)

        click.echo(f"    selectors ({len(selectors)})")
        for selector in sorted(selectors, key=lambda s: s.expressive):
            if selector.selector in new:
                click.echo(_sel_row("      ", "+ ", selector, "new"))
            elif moved.get(selector.selector, (None, None))[1] == source_id:
                origin = _facet_label(moved[selector.selector][0])
                click.echo(_sel_row("      ", "» ", selector, f"from {origin}"))
            else:
                click.echo(_sel_row("      ", "  ", selector))

        if status == "CHANGED":
            kept = {s.selector for s in selectors}
            lost = [s for s in (cur_by_facet.get(source_id) or []) if s.selector not in kept]
            if lost:
                click.echo(f"    no longer served ({len(lost)})")
                _lost_list(lost, moved)

    for source_id in cur_facets:
        if source_id in prop_facets:
            continue
        lost = cur_by_facet.get(source_id) or []
        click.echo("")
        click.echo(f"  {'REMOVED'.ljust(9)} {_facet_label(source_id).ljust(20)} {source_id}")
        click.echo(f"    selectors no longer served ({len(lost)})")
        _lost_list(lost, moved)

    if proposed.get("selectors"):
        addr = _short(proposed["selectors"].get("address"))
        click.echo("")
        click.echo(
            f"  {'GENERATED'.ljust(9)} selectors()          {addr}"
            f"  ·  returns {_plural(len(prop_owner), 'selector')}"
        )

    if proposed.get("migration"):
        cleared = sum(1 for s in removed if slot_address(storage.storage_values.get(s)))
        click.echo("")
        click.echo(f"MIGRATION  {_short(proposed['migration'].get('address'))}")
        click.echo(f"  installs {len(prop_owner)} selector routes, clears {cleared}")

    click.echo("")
    click.echo(
        f"{len(new)} new · {len(moved)} moved · "
        f"{_plural(len(removed), 'removed selector')}"
    )
