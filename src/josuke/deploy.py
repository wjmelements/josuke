import json
import pathlib
from collections import namedtuple
from os import environ

import click
import click_spinner
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .delegate import ContractSource, Delegate
from .erc8167 import SELECTORS_SELECTOR, generated_selectors, selectors_method
from .ethjsonrpc import chain_id, eth_get_code
from .evm import deployer_derived, evm_artifact
from .forge import get_forge_config
from .ledger import load_ledger, write_ledger
from .migration import Migration, SetDelegate
from .proc import run
from .selectors import Selector
from .storage import ProxyStorage, slot_address
from .worktree import SourceTrees

ZERO_ADDRESS = "0x" + "00" * 20

# A concrete, deployable facet resolved from a `facetSrc` pattern.
#   kind      "sol" | "evm"
#   path      repo-relative source path
#   contract  contract name (sol only; None for evm)
#   source_id the josuke.json key: "<path>:<Contract>" or "<path>.evm"
Facet = namedtuple("Facet", "kind path contract source_id")


def git_commit(root: pathlib.Path) -> str:
    if run(["git", "status", "--porcelain"], root).strip():
        click.echo("warning: uncommitted changes in the working tree", err=True)
    return run(["git", "rev-parse", "HEAD"], root).strip()


def universal_constructor() -> str:
    """`evm -C` with no code: the length-agnostic constructor prefix it prepends."""
    return run(["evm", "-C"], stdin="").strip().removeprefix("0x")


# -- facet resolution -------------------------------------------------------


def resolve_facets(facet_src: list, root: pathlib.Path) -> list:
    """Expand `facetSrc` patterns into concrete Facets. Requires a prior build."""
    out_dir = root / get_forge_config(root).get("out", "out")

    facets = []
    seen = set()
    for pattern in facet_src:
        if pattern.endswith(".evm"):
            found = [Facet("evm", pattern, None, pattern)]
        elif ":" in pattern:
            path, contract = pattern.split(":", 1)
            found = [Facet("sol", path, contract, pattern)]
        else:
            found = []
            for sol in sorted(root.glob(pattern)):
                rel = sol.relative_to(root).as_posix()
                for artifact in sorted((out_dir / sol.name).glob("*.json")):
                    obj = json.loads(artifact.read_text()).get("bytecode", {}).get("object", "")
                    if obj in ("", "0x"):
                        continue  # interface / abstract contract
                    facet = Facet("sol", rel, artifact.stem, f"{rel}:{artifact.stem}")
                    found.append(facet)
            if not found:
                raise click.ClickException(f"{pattern} matched no deployable contracts")

        for facet in found:
            if facet.source_id not in seen:
                seen.add(facet.source_id)
                facets.append(facet)
    return facets


def facet_from_source_id(source_id: str) -> Facet:
    if source_id.endswith(".evm"):
        return Facet("evm", source_id, None, source_id)
    path, contract = source_id.split(":", 1)
    return Facet("sol", path, contract, source_id)


def facet_abi(facet: Facet, root: pathlib.Path) -> list:
    if facet.kind == "sol":
        return json.loads(run(["forge", "inspect", facet.source_id, "abi", "--json"], root))
    return evm_artifact(root / facet.path, root)["abi"]


def facet_selectors(facet: Facet, root: pathlib.Path) -> list:
    return [
        Selector.from_abi(m) for m in facet_abi(facet, root) if m["type"] == "function"
    ]


# -- init bytecode --------------------------------------------------------


def constructor_inputs(source_id: str, root: pathlib.Path) -> list:
    abi = json.loads(run(["forge", "inspect", source_id, "abi", "--json"], root))
    ctor = [m for m in abi if m["type"] == "constructor"]
    return ctor[0]["inputs"] if ctor else []


def coerce_arg(abi_type: str, value):
    """Turn a JSON-shaped arg value into what eth_abi.encode expects."""
    if abi_type.endswith("]"):
        inner = abi_type[: abi_type.rindex("[")]
        return [coerce_arg(inner, v) for v in value]
    if abi_type.startswith("bytes") and isinstance(value, str):
        return bytes.fromhex(value.removeprefix("0x"))
    return value


def _prompt_arg(source_id: str, name: str, abi_type: str):
    raw = click.prompt(f"{source_id} constructor arg {name} ({abi_type})")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def facet_initcode(facet: Facet, root: pathlib.Path, recorded_args, prompt: bool = True):
    if facet.kind == "evm":
        return evm_artifact(root / facet.path, root)["initcode"], None

    initcode = run(["forge", "inspect", facet.source_id, "bytecode"], root).strip()
    initcode = initcode.removeprefix("0x")
    inputs = constructor_inputs(facet.source_id, root)
    if not inputs:
        return initcode, None

    recorded_args = recorded_args or {}

    def resolve(arg):
        if arg["name"] in recorded_args:
            return recorded_args[arg["name"]]
        if not prompt:
            raise click.ClickException(
                f"{facet.source_id}: no recorded value for constructor arg {arg['name']}"
            )
        return _prompt_arg(facet.source_id, arg["name"], arg["type"])

    args = {arg["name"]: resolve(arg) for arg in inputs}
    encoded_args = abi_encode(
        [arg["type"] for arg in inputs],
        [coerce_arg(arg["type"], args[arg["name"]]) for arg in inputs],
    ).hex()
    return initcode + encoded_args, args


def keccak_hex(data_hex: str) -> str:
    return "0x" + keccak(bytes.fromhex(data_hex.removeprefix("0x"))).hex()


def deploy_initcode(initcode_hex: str, root: pathlib.Path) -> tuple[str, str, str]:
    tx_hash = run(["cast", "send", "--async", "--create", "0x" + initcode_hex], root).strip()
    click.echo(f"deploying: tx {tx_hash} pending...")
    with click_spinner.spinner():
        out = run(["cast", "receipt", tx_hash, "--json"], root)
    receipt = json.loads(out)
    address = receipt.get("contractAddress")
    if not address:
        raise click.ClickException(f"cast receipt returned no contractAddress:\n{out}")
    return to_checksum_address(address), to_checksum_address(receipt["from"]), tx_hash


def code_hash(address: str) -> str:
    return keccak_hex(eth_get_code(address))


def verify_sourcify(
    facet: Facet, address: str, chain: str, root: pathlib.Path, creation_tx_hash: str | None = None
) -> None:
    contract = f"{facet.path}:{facet.contract}"
    click.echo(f"verifying {contract} on Sourcify")
    cmd = ["forge", "verify-contract", address, contract, "--chain", chain, "--verifier", "sourcify"]
    if creation_tx_hash:
        cmd += ["--creation-transaction-hash", creation_tx_hash]
    try:
        run(cmd, root)
    except click.ClickException as e:
        click.echo(f"warning: Sourcify verification failed for {contract}: {e}", err=True)


# -- migration script -----------------------------------------------------


def current_selectors(current: dict, current_tree: pathlib.Path | None) -> dict:
    """selector -> Selector for everything the installed facet set exposes, read
    from `current_tree`, the checkout of `current.gitCommit` (None when nothing is current)."""
    out = {}
    for source_id in current.get("facets", {}):
        try:
            selectors = facet_selectors(facet_from_source_id(source_id), current_tree)
        except click.ClickException:
            click.echo(
                f"warning: cannot determine selectors for {source_id}; not zeroed",
                err=True,
            )
            continue
        for selector in selectors:
            out[selector.selector] = selector
    return out


def selectors_runtime(facets: list, root: pathlib.Path) -> bytes | None:
    selector_lists = [facet_selectors(facet, root) for facet in facets]
    if any(s.selector == SELECTORS_SELECTOR for sels in selector_lists for s in sels):
        return None
    return selectors_method(generated_selectors(selector_lists))


def build_migration(
    proxy: str,
    facets: list,
    proposed_facets: dict,
    current: dict,
    current_tree: pathlib.Path | None,
    root: pathlib.Path,
    storage: ProxyStorage | None = None,
    selectors_impl: dict | None = None,
):
    """A Migration that points every proposed selector at its facet and zeroes
    selectors dropped since `current`. Returns None when nothing needs changing.

    `current_tree` is a checkout of `current.gitCommit` (None when nothing is
    current): a function deleted from a facet that is still in `facetSrc` only
    shows up in the old source.

    `storage` may be a pre-populated ProxyStorage to avoid re-querying slots.
    `selectors_impl` is the generated `selectors()` delegate record (if any);
    when given, `selectors()` is routed to it like any facet selector."""
    owner = {}  # selector -> Facet
    selectors = {}  # selector -> Selector
    for facet in facets:
        address = proposed_facets[facet.source_id]["address"]
        for selector in facet_selectors(facet, root):
            prior = owner.get(selector.selector)
            if prior and proposed_facets[prior.source_id]["address"] != address:
                raise click.ClickException(
                    f"selector {selector.selector} claimed by {prior.source_id} "
                    f"and {facet.source_id}"
                )
            owner[selector.selector] = facet
            selectors[selector.selector] = selector

    installed = current_selectors(current, current_tree)
    kept = set(owner)
    if selectors_impl:
        kept.add(SELECTORS_SELECTOR)  # re-pointed below, not removed
    removed = {s: installed[s] for s in installed if s not in kept}

    if storage is None:
        storage = ProxyStorage(proxy)
        probe = {**installed, **selectors}
        if selectors_impl:
            probe[SELECTORS_SELECTOR] = Selector(SELECTORS_SELECTOR, "selectors()")
        storage.fetch(list(probe.values()))

    setdelegates = []
    for sel in sorted(owner):
        slot = storage.storage_keys.get(sel)
        if slot is None:
            raise click.ClickException(f"could not resolve a storage slot for {sel}")
        facet = owner[sel]
        address = proposed_facets[facet.source_id]["address"]  # already checksummed by deploy_initcode
        if slot_address(storage.storage_values.get(sel)) == to_checksum_address(address):
            continue  # already routed correctly on-chain
        delegate = Delegate(address, ContractSource(facet.path, facet.contract or ""))
        setdelegates.append(SetDelegate(sel, slot, delegate))
    if selectors_impl:
        slot = storage.storage_keys.get(SELECTORS_SELECTOR)
        if slot is None:
            raise click.ClickException(f"could not resolve a storage slot for {SELECTORS_SELECTOR}")
        address = to_checksum_address(selectors_impl["address"])
        if slot_address(storage.storage_values.get(SELECTORS_SELECTOR)) != address:
            setdelegates.append(
                SetDelegate(SELECTORS_SELECTOR, slot, Delegate(address, ContractSource("selectors()", "")))
            )
    for sel in sorted(removed):
        if int(storage.storage_values.get(sel, "0x0") or "0x0", 16) == 0:
            continue  # already clear on chain
        setdelegates.append(
            SetDelegate(sel, storage.storage_keys[sel], Delegate(ZERO_ADDRESS, ContractSource("", "")))
        )

    # Each selector dispatches through its own storage slot, so the migration
    # must never SSTORE the same slot twice; a collision means slot detection
    # (evm -nx) returned the wrong slot for one of them.
    by_slot = {}
    for sd in setdelegates:
        clash = by_slot.get(sd.storage_key32)
        if clash is not None:
            raise click.ClickException(
                f"migration would write storage slot 0x{sd.storage_key32} for both "
                f"{clash} and {sd.selector}; selector storage-slot detection is wrong"
            )
        by_slot[sd.storage_key32] = sd.selector

    return Migration(setdelegates) if setdelegates else None


def deploy_migration(migration: Migration, prior: dict, root: pathlib.Path) -> dict:
    """Deploy the migration script unless `prior` already holds identical code."""
    runtime = migration.encode()
    if prior and prior.get("address"):
        onchain = eth_get_code(prior["address"])
        if bytes.fromhex(onchain.removeprefix("0x")) == runtime:
            return prior

    initcode = universal_constructor() + runtime.hex()
    click.echo("deploying migration")
    return {"address": deploy_initcode(initcode, root)[0]}


def deploy_selectors_impl(
    runtime: bytes, prior: dict | None, root: pathlib.Path, redeploy_all: bool = False
) -> dict:
    """Deploy the generated `selectors()` delegate unless `prior` already holds
    identical code (skipped by `redeploy_all`)."""
    if not redeploy_all and prior and prior.get("address"):
        onchain = eth_get_code(prior["address"])
        if bytes.fromhex(onchain.removeprefix("0x")) == runtime:
            return prior

    initcode = universal_constructor() + runtime.hex()
    click.echo("deploying selectors()")
    return {"address": deploy_initcode(initcode, root)[0]}


# -- orchestration ------------------------------------------------------


def run_deploy(ledger_path, redeploy_all: bool = False):
    if "ETH_RPC_URL" not in environ:
        raise click.ClickException("ETH_RPC_URL is not set")

    root = pathlib.Path.cwd()
    ledger = load_ledger(ledger_path)
    chain = chain_id()
    commit = git_commit(root)

    run(["forge", "build"], root)

    run_deployed: dict[str, dict] = {}  # initcodeHash -> facet entry, shared across proxies this run

    with SourceTrees(root) as trees:  # each `current.gitCommit`, for its selectors
        for entry in ledger:
            proxy = to_checksum_address(entry["address"])
            deployments = entry.setdefault("deployments", {})
            history = deployments.setdefault(chain, {})
            current = history.get("current", {})
            current_facets = current.get("facets", {})
            prior_proposed = history.get("proposed", {})
            prior_proposed_facets = prior_proposed.get("facets", {})

            facets = resolve_facets(entry["facetSrc"], root)
            proposed_facets = {}
            deployed = 0
            for facet in facets:
                recorded = (
                    prior_proposed_facets.get(facet.source_id)
                    or current_facets.get(facet.source_id)
                    or {}
                )
                initcode, args = facet_initcode(facet, root, recorded.get("constructorArgs"))
                initcode_hash = keccak_hex(initcode)

                # Reuse an existing deployment of this exact bytecode: the installed
                # facet if it still matches, otherwise one already staged in a prior
                # `proposed` run (keeps re-runs before promotion idempotent).
                live = current_facets.get(facet.source_id)
                staged = prior_proposed_facets.get(facet.source_id)
                if not redeploy_all:
                    if live and live.get("initcodeHash") == initcode_hash:
                        proposed_facets[facet.source_id] = live
                        run_deployed.setdefault(initcode_hash, live)
                        continue
                    if staged and staged.get("initcodeHash") == initcode_hash and staged.get("address"):
                        proposed_facets[facet.source_id] = staged
                        run_deployed.setdefault(initcode_hash, staged)
                        continue

                shared = run_deployed.get(initcode_hash)
                if shared and shared.get("address"):
                    click.echo(f"reusing {shared['address']} for {facet.source_id}")
                    proposed_facets[facet.source_id] = shared
                    continue

                click.echo(f"deploying {facet.source_id}")
                address, sender, tx_hash = deploy_initcode(initcode, root)
                if facet.kind == "sol":
                    verify_sourcify(facet, address, chain, root, tx_hash)
                facet_entry = {
                    "address": address,
                    "codehash": code_hash(address),
                    "initcodeHash": initcode_hash,
                }
                if args:
                    facet_entry["constructorArgs"] = args
                if deployer_derived(initcode, sender):
                    facet_entry["from"] = sender  # runtime depends on sender
                elif recorded.get("from"):
                    facet_entry["from"] = recorded["from"]
                proposed_facets[facet.source_id] = facet_entry
                run_deployed.setdefault(initcode_hash, facet_entry)
                deployed += 1

            proposed = {"gitCommit": commit, "facets": proposed_facets}

            runtime = selectors_runtime(facets, root)
            selectors_impl = None
            if runtime is not None:
                prior_selectors = prior_proposed.get("selectors") or current.get("selectors")
                selectors_impl = deploy_selectors_impl(runtime, prior_selectors, root, redeploy_all)
                proposed["selectors"] = selectors_impl

            current_tree = trees.get(current["gitCommit"]) if current_facets else None
            migration = build_migration(
                proxy, facets, proposed_facets, current, current_tree, root, selectors_impl=selectors_impl
            )
            if migration is not None:
                proposed["migration"] = deploy_migration(
                    migration, prior_proposed.get("migration"), root
                )
                history["proposed"] = proposed
            else:
                history.pop("proposed", None)  # nothing pending: already live on chain

            notes = [f"{deployed} facet(s) deployed"]
            if selectors_impl is not None:
                notes.append("selectors() unchanged" if selectors_impl is prior_selectors else "selectors() generated")
            notes.append("migration ready" if migration is not None else "no migration needed")
            click.echo(f"{proxy} chain {chain}: " + ", ".join(notes))

    write_ledger(ledger_path, ledger)
