#!/usr/bin/env python3.14

import pathlib

import click
from eth_utils import to_checksum_address

from .accept import run_accept
from .audit import run_audit
from .check import run_check
from .deploy import run_deploy
from .ledger import DEFAULT_LEDGER, load_ledger, parse_address, write_ledger
from .verify import run_verify

ledger_option = click.option(
    "-f",
    "--file",
    "ledger_path",
    type=click.Path(dir_okay=False, path_type=pathlib.Path),
    default=DEFAULT_LEDGER,
    show_default=True,
    help="Path to the josuke.json ledger.",
)


def unique_facets(facets: tuple) -> None:
    if len(set(facets)) != len(facets):
        raise click.ClickException("duplicate facet source")


@click.group()
def main():
    """josuke: automated, verifiable upgrades for ERC-8167 proxies."""


@main.command()
@ledger_option
def init(ledger_path):
    """Create an empty josuke.json ledger."""
    if ledger_path.exists():
        raise click.ClickException(f"{ledger_path} already exists")
    ledger_path.write_text("[]\n")
    click.echo(f"wrote {ledger_path}")


@main.command()
@click.argument("address")
@click.argument("facets", nargs=-1, required=True)
@ledger_option
def add(address, facets, ledger_path):
    """Add FACETS source patterns to proxy ADDRESS, registering it if new."""
    address = parse_address(address)
    ledger = load_ledger(ledger_path)
    unique_facets(facets)

    for entry in ledger:
        if to_checksum_address(entry["address"]) == address:
            break
    else:
        entry = {"address": address, "facetSrc": []}
        ledger.append(entry)

    facetSrc = entry["facetSrc"]
    already = set(facetSrc) & set(facets)
    if already:
        raise click.ClickException(f"{address} already has {', '.join(sorted(already))}")

    facetSrc.extend(facets)
    write_ledger(ledger_path, ledger)
    click.echo(f"added {len(facets)} facet source(s) to {address}")


@main.command()
@ledger_option
@click.option(
    "--all",
    "all_",
    is_flag=True,
    help="Redeploy every facet, even ones whose bytecode is unchanged.",
)
def deploy(ledger_path, all_):
    """Deploy changed and new facets to $ETH_RPC_URL, recording them under `proposed`.

    Uses `forge` to build and `cast` to broadcast, then verifies new .sol facets
    on Sourcify. The signing wallet is taken from Foundry's environment.
    """
    run_deploy(ledger_path, redeploy_all=all_)


@main.command()
@ledger_option
def verify(ledger_path):
    """Verify the ledger against $ETH_RPC_URL.

    Checks, per proxy: the `current` facets are built from their recorded commit
    and live on chain, the proxy dispatches to them; then the `proposed` facets
    match `facetSrc` and the recorded migration installs them and zeroes any
    removed selectors. Rebuilds each recorded gitCommit in a temporary worktree.
    """
    run_verify(ledger_path)


@main.command()
@ledger_option
def accept(ledger_path):
    """Accept a confirmed `proposed` deployment into `current`.

    Checks on chain that the migration ran: every `proposed` selector now routes
    to its facet and every selector dropped since `current` is cleared. Then
    merges `proposed` into `current` and removes it. Rebuilds each recorded
    gitCommit in a temporary worktree.
    """
    run_accept(ledger_path)


@main.command()
@ledger_option
@click.option(
    "--from-block",
    type=click.IntRange(min=0),
    default=None,
    help="Scan logs from this block instead of finding the proxy's deployment "
    "(which needs a node with historical state).",
)
def audit(ledger_path, from_block):
    """Audit every delegate each proxy has installed, per $ETH_RPC_URL.

    Reads the proxy's SelectorDelegated and DiamondDelegateCall logs from its
    deployment onward. Every delegate installed must be recorded in the ledger,
    and each `history` entry is verified against its recorded commit, which is
    rebuilt in a temporary worktree. Migrations are listed but not verified.
    """
    run_audit(ledger_path, from_block)


@main.command()
@ledger_option
@click.option("--chain", default=None, help="Check only this chain id (default: every chain in the ledger).")
@click.option(
    "--strict",
    is_flag=True,
    help="Also fail when HEAD differs from the staged deployment (`proposed`, else `current`).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "markdown"]),
    default="text",
    show_default=True,
    help="markdown suits a PR comment or $GITHUB_STEP_SUMMARY.",
)
def check(ledger_path, chain, strict, fmt):
    """Check the source in the working tree against the ledger, offline.

    Builds with `forge` and needs no RPC or keys. Fails when the ledger breaks
    the schema, `facetSrc` doesn't resolve, two facets export one selector, or
    a facet's recorded constructor args can't produce its creation code. Lists
    each facet as new, changed, unchanged or removed against `current`.
    """
    run_check(ledger_path, chain, strict, fmt)
