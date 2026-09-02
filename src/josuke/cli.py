#!/usr/bin/env python3.14

import pathlib

import click
from eth_utils import to_checksum_address

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
def deploy(ledger_path):
    """Deploy changed and new facets to $ETH_RPC_URL, recording them under `proposed`.

    Uses `forge` to build and `cast` to broadcast; the signing wallet is taken
    from Foundry's environment (ETH_KEYSTORE_ACCOUNT, ETH_FROM, ...).
    """
    run_deploy(ledger_path)


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
