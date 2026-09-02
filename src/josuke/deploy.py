import json
import pathlib
import subprocess
from collections import namedtuple
from os import environ

import click
import requests
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from .delegate import ContractSource, Delegate
from .forge import get_forge_config
from .ledger import load_ledger, write_ledger
from .migration import Migration, SetDelegate
from .selectors import Selector
from .storage import ProxyStorage

ZERO_ADDRESS = "0x" + "00" * 20

# A concrete, deployable facet resolved from a `facetSrc` pattern.
#   kind      "sol" | "evm"
#   path      repo-relative source path
#   contract  contract name (sol only; None for evm)
#   source_id the josuke.json key: "<path>:<Contract>" or "<path>.evm"
Facet = namedtuple("Facet", "kind path contract source_id")


def _run(cmd: list, root: pathlib.Path | None = None, stdin: str = None) -> str:
    try:
        return subprocess.run(
            cmd, cwd=root, input=stdin, capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError:
        raise click.ClickException(f"{cmd[0]}: command not found")
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"{' '.join(cmd[:3])} failed:\n{e.stderr.strip()}")


def rpc(method: str, params: list):
    resp = requests.post(
        environ["ETH_RPC_URL"],
        json={"id": 1, "jsonrpc": "2.0", "method": method, "params": params},
    )
    if resp.status_code != 200:
        raise click.ClickException(f"{method}: HTTP {resp.status_code}")
    body = resp.json()
    if body.get("error"):
        raise click.ClickException(f"{method}: {body['error']}")
    return body["result"]


def chain_id() -> str:
    """Decimal chain id string, from `cast` when available, else `eth_chainId`."""
    try:
        out = subprocess.run(
            ["cast", "chain-id"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return str(int(out))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return str(int(rpc("eth_chainId", []), 16))


def git_commit(root: pathlib.Path) -> str:
    if _run(["git", "status", "--porcelain"], root).strip():
        click.echo("warning: uncommitted changes in the working tree", err=True)
    return _run(["git", "rev-parse", "HEAD"], root).strip()


def universal_constructor() -> str:
    """`evm -C` with no code: the length-agnostic constructor prefix it prepends."""
    return _run(["evm", "-C"], stdin="").strip().removeprefix("0x")


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
        return json.loads(_run(["forge", "inspect", facet.source_id, "abi", "--json"], root))

    # A raw-bytecode facet still needs an ABI to know its selectors; take it from
    # a Foundry artifact whose name matches the file.
    # FIXME: `forge build` won't build the .evm artifact (out/<name>.evm/<name>.json);
    # per the Makefile in ~/projects/erc8167 it's assembled with `evm` and merged
    # with the ABI of the matching src/interfaces/<name>.sol. Build it here.
    stem = pathlib.Path(facet.path).stem
    out_dir = root / get_forge_config(root).get("out", "out")
    for artifact in sorted(out_dir.glob("**/*.json")):
        if artifact.stem.lower() == stem.lower():
            data = json.loads(artifact.read_text())
            if isinstance(data, list):
                return data
            if "abi" in data:
                return data["abi"]
    raise click.ClickException(f"no Foundry artifact with an ABI found for {facet.source_id}")


def facet_selectors(facet: Facet, root: pathlib.Path) -> list:
    return [
        Selector.from_abi(m) for m in facet_abi(facet, root) if m["type"] == "function"
    ]


# -- init bytecode --------------------------------------------------------


def constructor_inputs(source_id: str, root: pathlib.Path) -> list:
    abi = json.loads(_run(["forge", "inspect", source_id, "abi", "--json"], root))
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
    """Return (initcode_hex, constructor_args | None) for `facet` at HEAD.

    With `prompt` false, a constructor arg missing from `recorded_args` is an
    error instead of an interactive prompt (used when verifying)."""
    if facet.kind == "evm":
        # FIXME: this treats the .evm file as raw hex, but ERC-8167 ships .evm as
        # evm-assembler source. Per the Makefile in ~/projects/erc8167, the
        # initcode is `evm -c <file>` (or `evm <file>` when the name contains
        # "constructor"), and `forge build` does not produce these artifacts.
        # Assemble it here (and fall back to raw hex only if that fails).
        raw = (root / facet.path).read_text().strip()
        return raw.removeprefix("0x"), None

    initcode = _run(["forge", "inspect", facet.source_id, "bytecode"], root).strip()
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
    encoded = abi_encode(
        [arg["type"] for arg in inputs],
        [coerce_arg(arg["type"], args[arg["name"]]) for arg in inputs],
    ).hex()
    return initcode + encoded, args


def keccak_hex(data_hex: str) -> str:
    return "0x" + keccak(bytes.fromhex(data_hex.removeprefix("0x"))).hex()


def deploy_initcode(initcode_hex: str, root: pathlib.Path) -> str:
    """Broadcast a creation transaction via cast; return the new contract address."""
    out = _run(["cast", "send", "--create", "0x" + initcode_hex, "--json"], root)
    address = json.loads(out).get("contractAddress")
    if not address:
        raise click.ClickException(f"cast send returned no contractAddress:\n{out}")
    return to_checksum_address(address)


def code_hash(address: str) -> str:
    return keccak_hex(rpc("eth_getCode", [address, "latest"]))


# -- migration script -----------------------------------------------------


def current_selectors(current: dict, root: pathlib.Path) -> dict:
    """selector -> Selector for everything the installed facet set exposes."""
    out = {}
    for source_id in current.get("facets", {}):
        try:
            selectors = facet_selectors(facet_from_source_id(source_id), root)
        except click.ClickException:
            click.echo(
                f"warning: cannot determine selectors for {source_id}; not zeroed",
                err=True,
            )
            continue
        for selector in selectors:
            out[selector.selector] = selector
    return out


def build_migration(
    proxy: str,
    facets: list,
    proposed_facets: dict,
    current: dict,
    root: pathlib.Path,
    storage: ProxyStorage | None = None,
):
    """A Migration that points every proposed selector at its facet and zeroes
    selectors dropped since `current`. Returns None when nothing needs changing.

    `storage` may be a pre-populated ProxyStorage to avoid re-querying slots."""
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

    installed = current_selectors(current, root)
    removed = {s: installed[s] for s in installed if s not in owner}

    if storage is None:
        storage = ProxyStorage(proxy)
        storage.fetch(list({**installed, **selectors}.values()))

    setdelegates = []
    for sel in sorted(owner):
        slot = storage.storage_keys.get(sel)
        if slot is None:
            raise click.ClickException(f"could not resolve a storage slot for {sel}")
        facet = owner[sel]
        delegate = Delegate(
            proposed_facets[facet.source_id]["address"],  # already checksummed by deploy_initcode
            ContractSource(facet.path, facet.contract or ""),
        )
        setdelegates.append(SetDelegate(sel, slot, delegate))
    for sel in sorted(removed):
        if int(storage.storage_values.get(sel, "0x0") or "0x0", 16) == 0:
            continue  # already clear on chain
        setdelegates.append(
            SetDelegate(sel, storage.storage_keys[sel], Delegate(ZERO_ADDRESS, ContractSource("", "")))
        )

    return Migration(setdelegates) if setdelegates else None


def deploy_migration(migration: Migration, prior: dict, root: pathlib.Path) -> dict:
    """Deploy the migration script unless `prior` already holds identical code."""
    runtime = migration.encode()
    if prior and prior.get("address"):
        onchain = rpc("eth_getCode", [prior["address"], "latest"])
        if bytes.fromhex(onchain.removeprefix("0x")) == runtime:
            return prior

    initcode = universal_constructor() + runtime.hex()
    click.echo("deploying migration")
    return {"address": deploy_initcode(initcode, root)}


# -- orchestration ------------------------------------------------------


def run_deploy(ledger_path):
    if "ETH_RPC_URL" not in environ:
        raise click.ClickException("ETH_RPC_URL is not set")

    root = pathlib.Path.cwd()
    ledger = load_ledger(ledger_path)
    chain = chain_id()
    commit = git_commit(root)

    _run(["forge", "build"], root)

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
            if live and live.get("initcodeHash") == initcode_hash:
                proposed_facets[facet.source_id] = live
                continue
            if staged and staged.get("initcodeHash") == initcode_hash and staged.get("address"):
                proposed_facets[facet.source_id] = staged
                continue

            click.echo(f"deploying {facet.source_id}")
            address = deploy_initcode(initcode, root)
            facet_entry = {
                "address": address,
                "codehash": code_hash(address),
                "initcodeHash": initcode_hash,
            }
            if args:
                facet_entry["constructorArgs"] = args
            proposed_facets[facet.source_id] = facet_entry
            deployed += 1

        proposed = {"gitCommit": commit, "facets": proposed_facets}
        migration = build_migration(proxy, facets, proposed_facets, current, root)
        if migration is not None:
            proposed["migration"] = deploy_migration(
                migration, prior_proposed.get("migration"), root
            )
        history["proposed"] = proposed

        click.echo(
            f"{proxy} chain {chain}: {deployed} facet(s) deployed, "
            f"{'migration ready' if migration is not None else 'no migration needed'}"
        )

    write_ledger(ledger_path, ledger)
