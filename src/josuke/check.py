"""`josuke check`: what the source in the working tree would do to the ledger,
worked out offline, for pull requests.

Needs only the build: no RPC, no keys, no worktrees. It trusts the ledger's
recorded hashes; `josuke verify` is what ties those to the chain.

Blocking: the ledger breaks the schema or lists a proxy twice; `facetSrc` does
not resolve to buildable facets; two facets `deploy` would put at different
addresses export one selector; two facets declare different variables over
the same storage bytes; a facet's recorded constructor args don't encode.

Reported, and blocking only with `--strict`: HEAD differs from the deployment
the ledger stages (`proposed`, else `current`).
"""

import json
import pathlib
from collections import Counter
from os import environ

import click
from eth_abi.exceptions import EncodingError
from eth_utils import to_checksum_address

from .deploy import (
    constructor_inputs,
    existing_deployment,
    facet_initcode,
    facet_selectors,
    keccak_hex,
    recorded_facet,
    resolve_facets,
)
from .erc8167 import SELECTORS_SELECTOR
from .forge import get_forge_config
from .ledger import load_ledger, validate_ledger
from .proc import run

# Compiler settings that change bytecode, shown so a hash difference can be
# traced to the build rather than the source.
BUILD_SETTINGS = ("solc", "via_ir", "evm_version", "bytecode_hash", "cbor_metadata")

# What `forge inspect` says when an artifact was built without the storage layout.
STALE_LAYOUT = "storage layout missing from artifact"


class Findings:
    def __init__(self):
        self.failures: list[str] = []
        self.drift: list[str] = []

    def fail(self, message: str) -> None:
        if message not in self.failures:  # the same fault shows up once per chain
            self.failures.append(message)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _head(root: pathlib.Path) -> str:
    try:
        commit = run(["git", "rev-parse", "--short", "HEAD"], root).strip()
        dirty = run(["git", "status", "--porcelain"], root).strip()
    except click.ClickException:
        return "HEAD unknown (not a git checkout)"
    return f"HEAD {commit}" + (" + uncommitted changes" if dirty else "")


def _build_context(root: pathlib.Path) -> str:
    config = get_forge_config(root)
    parts = [f"profile {environ.get('FOUNDRY_PROFILE', 'default')}"]
    optimizer = f"optimizer {str(config.get('optimizer')).lower()}"
    if config.get("optimizer"):
        optimizer += f" ({config.get('optimizer_runs')} runs)"
    parts.append(optimizer)
    for key in BUILD_SETTINGS:
        value = config.get(key)
        parts.append(f"{key} {str(value).lower() if isinstance(value, bool) else value}")
    return " · ".join(parts)


def _storage_layout(facet, root: pathlib.Path) -> dict | None:
    """solc's storage layout for a Solidity facet; None for a `.evm` facet, which has none."""
    if facet.kind != "sol":
        return None
    return json.loads(run(["forge", "inspect", facet.source_id, "storageLayout", "--json"], root))


def _resolve(entry: dict, root: pathlib.Path, findings: Findings) -> dict | None:
    """source_id -> (Facet, [Selector], storage layout) at HEAD, or None if
    `facetSrc` doesn't resolve."""
    proxy = to_checksum_address(entry["address"])
    try:
        facets = resolve_facets(entry["facetSrc"], root)
    except click.ClickException as e:
        findings.fail(f"{proxy} facetSrc: {e.message}")
        return None
    out = {}
    for facet in facets:
        try:
            selectors = facet_selectors(facet, root)
        except click.ClickException as e:
            findings.fail(f"{proxy} {facet.source_id}: cannot read its ABI: {e.message}")
            continue
        try:
            out[facet.source_id] = (facet, selectors, _storage_layout(facet, root))
        except click.ClickException as e:
            findings.fail(f"{proxy} {facet.source_id}: cannot read its storage layout: {e.message}")
    return out if len(out) == len(facets) else None


def _position(start: int) -> str:
    slot, offset = divmod(start, 32)
    return f"slot {slot}" + (f" offset {offset}" if offset else "")


def _check_storage(proxy: str, resolved: dict, findings: Findings) -> str:
    """Every facet runs against the proxy's one storage, so storage bytes that two
    facets both declare must hold the same variable (name and type) in both.

    Sees only declared state variables: ERC-7201 namespaced structs, assembly
    `sstore`s and `.evm` facets are invisible to it."""
    declared = []  # (first byte, end byte, source_id, name, type)
    unseen = []
    for source_id, (_, _, layout) in resolved.items():
        if layout is None:
            unseen.append(source_id)
            continue
        types = layout.get("types") or {}
        for var in layout.get("storage") or []:
            kind = types[var["type"]]
            start = int(var["slot"]) * 32 + var["offset"]
            declared.append((start, start + int(kind["numberOfBytes"]), source_id, var["label"], kind["label"]))

    declared.sort()
    shared = set()  # slots declared identically by more than one facet
    for i, a in enumerate(declared):
        for b in declared[i + 1 :]:
            if b[0] >= a[1]:
                break  # sorted by first byte: nothing later overlaps `a`
            if a[2] == b[2]:
                continue
            if (a[0], a[1], a[3], a[4]) == (b[0], b[1], b[3], b[4]):
                shared.add(a[0] // 32)
                continue
            findings.fail(
                f"{proxy} storage {_position(b[0])}: {b[2]} declares {b[4]} {b[3]} "
                f"over {a[2]}'s {a[4]} {a[3]} at {_position(a[0])}"
            )

    checked = len(resolved) - len(unseen)
    line = f"  storage      {_plural(checked, 'facet')} checked, {_plural(len(shared), 'slot')} shared"
    if unseen:
        line += f"; not visible: {', '.join(unseen)}"
    return line


def _initcode_hash(facet, root, recorded: dict, proxy: str, findings: Findings) -> tuple[str | None, list[str]]:
    """(keccak of the creation code `deploy` would send, constructor args it
    would prompt for). The hash is None when args are missing, or on a failure."""
    source_id = facet.source_id
    args = recorded.get("constructorArgs") or {}
    try:
        if facet.kind == "sol":
            missing = [arg["name"] for arg in constructor_inputs(source_id, root) if arg["name"] not in args]
            if missing:
                return None, missing
        initcode, _ = facet_initcode(facet, root, args, prompt=False)
    except click.ClickException as e:
        findings.fail(f"{proxy} {source_id}: {e.message}")
        return None, []
    except (EncodingError, ValueError, TypeError, KeyError) as e:
        findings.fail(f"{proxy} {source_id}: recorded constructorArgs do not encode: {e}")
        return None, []
    if "__$" in initcode:
        findings.fail(f"{proxy} {source_id}: creation code has unlinked library references")
        return None, []
    try:
        code = bytes.fromhex(initcode)
    except ValueError:
        findings.fail(f"{proxy} {source_id}: creation code is not hex")
        return None, []
    if not code:
        findings.fail(f"{proxy} {source_id}: no creation code (an interface or abstract contract?)")
        return None, []
    return keccak_hex(initcode), []


def _differences(head: dict, reference: dict) -> list[str]:
    """How HEAD's source_id -> initcodeHash differs from a recorded facet set."""
    out = []
    for source_id, initcode_hash in head.items():
        if source_id not in reference:
            out.append(f"adds {source_id}")
        elif reference[source_id].get("initcodeHash") != initcode_hash:
            out.append(f"changes {source_id}")
    out.extend(f"drops {source_id}" for source_id in reference if source_id not in head)
    return out


def _check_proxy(entry, chain, resolved, storage_line, root, run_deployed, findings) -> list[str]:
    """Check one proxy on one chain (None: nothing deployed anywhere yet) and
    render its section of the report."""
    proxy = to_checksum_address(entry["address"])
    history = entry.get("deployments", {}).get(chain, {}) if chain else {}
    current = history.get("current") or {}
    proposed = history.get("proposed") or {}
    current_facets = current.get("facets", {})
    proposed_facets = proposed.get("facets", {})

    if chain is None:
        header = f"{proxy}  ·  nothing deployed yet"
    elif current:
        header = f"{proxy}  chain {chain}  ·  vs current {current['gitCommit'][:7]}"
    else:
        header = f"{proxy}  chain {chain}  ·  nothing current"
    lines = ["", header]

    head_hashes = {}  # source_id -> initcodeHash, or None while deploy still needs its args
    owners = {}  # selector -> (source_id, deployment address)
    fresh = set()  # initcodeHashes `deploy` would deploy
    prompts = 0  # facets whose constructor args `deploy` would ask for
    for source_id, (facet, selectors, _) in resolved.items():
        initcode_hash, missing = _initcode_hash(
            facet, root, recorded_facet(source_id, current_facets, proposed_facets), proxy, findings
        )
        note = ""
        if missing:
            # Its bytecode can't be known until deploy supplies the args, so it
            # gets a deployment of its own and counts as new or changed.
            prompts += 1
            status = "CHANGED" if source_id in current_facets else "NEW"
            address = f"new {source_id}"
            head_hashes[source_id] = None
            note = f"  deploy asks for {', '.join(missing)}"
        elif initcode_hash is None:
            status, address = "ERROR", f"unbuilt {source_id}"
        else:
            head_hashes[source_id] = initcode_hash
            existing, _ = existing_deployment(
                source_id, initcode_hash, current_facets, proposed_facets, run_deployed
            )
            if existing is None:
                # Stands in for the address `deploy` would get; later sources and
                # proxies with the same bytecode share it, as they would on chain.
                existing = {"address": f"new {initcode_hash}"}
                run_deployed.setdefault(initcode_hash, existing)
                fresh.add(initcode_hash)
            address = existing.get("address")
            if source_id not in current_facets:
                status = "NEW"
            elif current_facets[source_id].get("initcodeHash") != initcode_hash:
                status = "CHANGED"
            else:
                status = "UNCHANGED"

        # `build_migration` refuses a selector claimed at two addresses.
        for selector in selectors:
            prior = owners.get(selector.selector)
            if prior and prior[1] != address:
                findings.fail(
                    f"{proxy} selector {selector.selector} {selector.expressive} "
                    f"is exported by both {prior[0]} and {source_id}"
                )
            owners[selector.selector] = (source_id, address)
        lines.append(f"  {status.ljust(10)} {source_id.ljust(60)} {_plural(len(selectors), 'selector')}{note}")

    for source_id in current_facets:
        if source_id not in resolved:
            lines.append(f"  {'REMOVED'.ljust(10)} {source_id}")

    if SELECTORS_SELECTOR in owners:
        lines.append(
            f"  selectors()  served by {owners[SELECTORS_SELECTOR][0]} (what it returns is not checked)"
        )
    else:
        lines.append(f"  selectors()  generated · returns {_plural(len(owners) + 1, 'selector')}")
    lines.append(storage_line)

    if len(head_hashes) == len(resolved):
        if proposed:
            label = f"proposed {proposed['gitCommit'][:7]}"
            reference = proposed_facets
        else:
            label = f"current {current['gitCommit'][:7]}" if current else None
            reference = current_facets
        differences = _differences(head_hashes, reference) if label else []
        if not label:
            lines.append("  staged       nothing recorded to compare with")
        elif differences:
            lines.append(f"  staged       HEAD differs from {label}: {'; '.join(differences)}")
            findings.drift.append(f"{proxy} chain {chain}: HEAD differs from {label}: {'; '.join(differences)}")
        else:
            lines.append(f"  staged       HEAD matches {label}")

    deploys = _plural(len(fresh) + prompts, "facet")
    lines.append(
        f"  deploy       would deploy {deploys}"
        + (f", asking for constructor args of {prompts}" if prompts else "")
    )
    return lines


def check_ledger(ledger_path, root: pathlib.Path, chain: str | None) -> tuple[list[str], Findings]:
    findings = Findings()
    lines = [f"josuke check · {ledger_path} · {_head(root)}"]
    ledger = load_ledger(ledger_path)

    for error in validate_ledger(ledger):
        findings.fail(f"schema: {error}")
    if findings.failures:
        return lines, findings  # nothing below can trust a malformed ledger

    for address, count in Counter(to_checksum_address(e["address"]) for e in ledger).items():
        if count > 1:
            findings.fail(f"{address} is listed {count} times")

    # Storage layouts aren't in artifacts by default; the extra output leaves bytecode unchanged.
    build = ["forge", "build", "--extra-output", "storageLayout"]
    try:
        run(build, root)
        lines.append(f"build  {_build_context(root)}")
    except click.ClickException as e:
        findings.fail(f"build: {e.message}")
        return lines, findings

    resolving = Findings()
    resolved = [_resolve(entry, root, resolving) for entry in ledger]
    if any(STALE_LAYOUT in failure for failure in resolving.failures):
        # forge's cache can claim artifacts are current while they lack the layout.
        lines.append("build  artifacts had no storage layout although the cache said current; rebuilt with --force")
        try:
            run([*build, "--force"], root)
        except click.ClickException as e:
            findings.fail(f"build: {e.message}")
            return lines, findings
        resolving = Findings()
        resolved = [_resolve(entry, root, resolving) for entry in ledger]
    findings.failures.extend(resolving.failures)
    storage_lines = [
        _check_storage(to_checksum_address(entry["address"]), facets, findings) if facets is not None else None
        for entry, facets in zip(ledger, resolved)
    ]

    recorded_chains = {c for entry in ledger for c in entry.get("deployments", {})}
    chains = [chain] if chain else sorted(recorded_chains, key=int) or [None]
    for c in chains:
        run_deployed: dict = {}  # initcodeHash -> record, shared across proxies as `deploy` does
        for entry, facets, storage_line in zip(ledger, resolved, storage_lines):
            if facets is None:
                lines.extend(["", f"{to_checksum_address(entry['address'])}  ·  facetSrc does not resolve"])
                continue
            lines.extend(_check_proxy(entry, c, facets, storage_line, root, run_deployed, findings))
    return lines, findings


def _render(lines: list[str], findings: Findings, strict: bool) -> list[str]:
    blocking = findings.failures + (findings.drift if strict else [])
    out = list(lines)
    out.append("")
    if blocking:
        out.append(f"FAILED  {_plural(len(blocking), 'finding')}")
        out.extend(f"  - {message}" for message in blocking)
    else:
        out.append("OK  no blocking findings")
    if findings.drift and not strict:
        out.append(f"  ({_plural(len(findings.drift), 'staged-deployment difference')}; blocking with --strict)")
    return out


def run_check(ledger_path, chain: str | None = None, strict: bool = False, fmt: str = "text") -> None:
    root = pathlib.Path.cwd()
    lines, findings = check_ledger(ledger_path, root, chain)
    report = _render(lines, findings, strict)
    blocking = len(findings.failures) + (len(findings.drift) if strict else 0)

    if fmt == "markdown":
        verdict = f"failed, {_plural(blocking, 'finding')}" if blocking else "passed"
        click.echo(f"### josuke check `{ledger_path}`: {verdict}\n")
        click.echo("```text")
        click.echo("\n".join(report))
        click.echo("```")
    else:
        click.echo("\n".join(report))

    if blocking:
        raise click.ClickException(f"josuke check: {_plural(blocking, 'blocking finding')}")
