"""`josuke abi`: the proxy's ABI, merged from the facets its `facetSrc` resolves to."""

import json
import pathlib

import click
from eth_utils import to_checksum_address

from .deploy import facet_abi, resolve_facets
from .erc8167 import SELECTORS_SELECTOR
from .ledger import load_ledger
from .proc import run
from .selectors import Selector

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


def merge_abis(abis: dict) -> list:
    """Merge {source_id: abi} into one ABI: each function once per selector (the
    first facet's, warning of any other), each event and error once, then
    `selectors()` if no facet exports it."""
    merged = []
    functions = {}  # selector -> (source_id, entry)
    seen = set()
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
            elif _key(item) in seen:
                continue
            seen.add(_key(item))
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
