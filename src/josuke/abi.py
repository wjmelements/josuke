"""`josuke abi`: the proxy's ABI, merged from the facets its `facetSrc` resolves to."""

import json
import pathlib

import click
from eth_utils import to_checksum_address

from .deploy import facet_abi, resolve_facets
from .erc8167 import SELECTORS_SELECTOR
from .ledger import load_ledger
from .proc import run
from .selectors import Selector, canonical_type

# The generated `selectors()` delegate, added when no facet implements it.
SELECTORS_ABI = {
    "type": "function",
    "name": "selectors",
    "inputs": [],
    "outputs": [{"name": "", "type": "bytes4[]", "internalType": "bytes4[]"}],
    "stateMutability": "view",
}

# The proxy dispatches by selector, so a facet's constructor, fallback and
# receive are never reached through it.
UNREACHABLE = {"constructor", "fallback", "receive"}


def _key(item: dict) -> str:
    return json.dumps(item, sort_keys=True)


def _signature(item: dict) -> str:
    """The event's or error's signature, marking which event inputs are indexed."""
    inputs = ",".join(canonical_type(arg) + (" indexed" if arg.get("indexed") else "") for arg in item["inputs"])
    return f"{item['name']}({inputs})" + (" anonymous" if item.get("anonymous") else "")


def merge_abis(abis: dict) -> list:
    """Merge {source_id: abi} into one ABI: each function once per selector, each
    event once per signature and indexing, and each error once per signature (the
    first facet's, warning of any other), then `selectors()` if no facet exports it."""
    merged = []
    functions = {}  # selector -> (source_id, entry)
    declared = {}  # (type, signature) -> (source_id, entry), for events and errors
    for source_id, abi in abis.items():
        for item in abi:
            if item["type"] in UNREACHABLE:
                continue
            if item["type"] == "function":
                selector = Selector.from_abi(item)
                prior = functions.get(selector.selector)
                if prior is not None:
                    # `deploy` refuses this unless both facets share bytecode, which
                    # would take a build of each to tell; `check` makes that call.
                    keeping = f"; keeping {prior[0]}" if _key(prior[1]) != _key(item) else ""
                    click.echo(
                        f"warning: selector {selector.selector} {selector.expressive} is exported "
                        f"by both {prior[0]} and {source_id}{keeping}",
                        err=True,
                    )
                    continue
                functions[selector.selector] = (source_id, item)
            else:
                key = (item["type"], _signature(item))
                prior = declared.get(key)
                if prior is not None:
                    # Same encoding, so the names differ only in decoding.
                    if _key(prior[1]) != _key(item):
                        click.echo(
                            f"warning: {key[0]} {key[1]} is declared differently by "
                            f"{prior[0]} and {source_id}; keeping {prior[0]}",
                            err=True,
                        )
                    continue
                declared[key] = (source_id, item)
            merged.append(item)
    if SELECTORS_SELECTOR not in functions:
        merged.append(SELECTORS_ABI)
    return merged


def run_abi(ledger_path, address: str) -> None:
    root = pathlib.Path.cwd()
    for entry in load_ledger(ledger_path):
        if to_checksum_address(entry["address"]) == address:
            break
    else:
        raise click.ClickException(f"{address} is not in {ledger_path}")

    run(["forge", "build"], root)
    facets = resolve_facets(entry["facetSrc"], root)
    abi = merge_abis({facet.source_id: facet_abi(facet, root) for facet in facets})
    click.echo(json.dumps(abi, indent=2))
