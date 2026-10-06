"""`josuke check`: what the source in the working tree would do to the ledger,
worked out offline, for pull requests.

Needs only the build and git history: no RPC, no keys. It trusts the ledger's
recorded hashes; `josuke verify` is what ties those to the chain.

Blocking: the ledger breaks the schema or lists a proxy twice; `facetSrc` does
not resolve to buildable facets; two facets `deploy` would put at different
addresses export one selector; two facets declare different variables over
the same storage bytes; storage that a recorded deployment (`legacy`,
`current`, `proposed` or `history`) declares reads back differently at HEAD;
a facet's recorded constructor args don't encode.

Warned about: a variable renamed, or dropped with its data left behind.

Reported, and blocking only with `--strict`: HEAD differs from the deployment
the ledger stages (`proposed`, else `current`).
"""

import pathlib
import re
from collections import Counter, namedtuple
from os import environ

import click
from eth_abi.exceptions import EncodingError
from eth_utils import to_checksum_address

from .deploy import (
    constructor_inputs,
    existing_deployment,
    facet_from_source_id,
    facet_initcode,
    facet_selectors,
    keccak_hex,
    recorded_facet,
    resolve_facets,
)
from .erc8167 import SELECTORS_SELECTOR
from .forge import get_forge_config
from .layout import compiler_version, storage_layouts
from .ledger import load_ledger, validate_ledger
from .proc import run
from .worktree import SourceTrees

# Compiler settings that change bytecode, shown so a hash difference can be
# traced to the build rather than the source.
BUILD_SETTINGS = ("solc", "via_ir", "evm_version", "bytecode_hash", "cbor_metadata")

_ELEMENTARY = re.compile(r"^(u?int\d*|bool|address|bytes\d+)$")

# One declared state variable, covering bytes [start, end) of storage counted
# from slot 0; `type` is solc's label, `shape` the comparable form of the type.
Decl = namedtuple("Decl", "start end source label type shape")


class Findings:
    def __init__(self):
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.drift: list[str] = []

    def fail(self, message: str) -> None:
        if message not in self.failures:  # the same fault shows up once per chain
            self.failures.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


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


def _built_solc_version(root: pathlib.Path) -> str | None:
    """The solc version of the build in `root`, for projects that don't pin one."""
    out = root / get_forge_config(root).get("out", "out")
    return next(filter(None, map(compiler_version, sorted(out.glob("*/*.json")))), None)


def _resolve(entry: dict, root: pathlib.Path, solc_version: str | None, findings: Findings) -> tuple[dict, list[Decl]] | None:
    """(source_id -> (Facet, [Selector], storage layout or None for `.evm`),
    the storage they declare) at HEAD, or None if `facetSrc` doesn't resolve."""
    proxy = to_checksum_address(entry["address"])
    try:
        facets = resolve_facets(entry["facetSrc"], root)
    except click.ClickException as e:
        findings.fail(f"{proxy} facetSrc: {e.message}")
        return None
    selectors = {}
    for facet in facets:
        try:
            selectors[facet.source_id] = facet_selectors(facet, root)
        except click.ClickException as e:
            findings.fail(f"{proxy} {facet.source_id}: cannot read its ABI: {e.message}")
    if len(selectors) != len(facets):
        return None
    try:
        layouts, namespaces = storage_layouts(root, facets, solc_version)
    except click.ClickException as e:
        findings.fail(f"{proxy}: cannot read storage layouts: {e.message}")
        return None
    resolved = {f.source_id: (f, selectors[f.source_id], layouts.get(f.source_id)) for f in facets}
    return resolved, _declarations(layouts | namespaces)


def _position(start: int) -> str:
    slot, offset = divmod(start, 32)
    return f"slot {slot if slot < 2**64 else hex(slot)}" + (f" offset {offset}" if offset else "")


def _shape(types: dict, type_id: str) -> tuple:
    """A type as it sits in storage, comparable across commits: solc's type ids
    carry AST ids, and struct labels the contract that happens to define them."""
    t = types[type_id]
    size = int(t["numberOfBytes"])
    encoding = t["encoding"]
    if encoding == "mapping":
        return ("mapping", size, _shape(types, t["key"]), _shape(types, t["value"]))
    if encoding == "bytes":
        return ("bytes", size)
    if encoding == "dynamic_array":
        return ("dynamic_array", size, _shape(types, t["base"]))
    if "members" in t:
        members = tuple((m["label"], int(m["slot"]), m["offset"], _shape(types, m["type"])) for m in t["members"])
        return ("struct", size, members)
    if "base" in t:
        return ("array", size, _shape(types, t["base"]))
    label = t.get("underlying", t["label"]).removesuffix(" payable")
    if label.startswith("contract "):
        label = "address"
    elif label.startswith("enum "):
        label = "enum"
    elif not _ELEMENTARY.match(label):
        label = "value"  # a user-defined value type
    return ("value", size, label)


def _fit(old: tuple, new: tuple, notes: set) -> bool:
    """Whether storage written as shape `old` reads back correctly as `new`.
    Adds "renamed" or "grown" to `notes` for the differences that allows."""
    if old == new:
        return True
    kind = old[0]
    if kind != new[0]:
        return False
    if kind == "mapping":
        # Every value has slots of its own, so a value may grow.
        return old[2] == new[2] and _fit(old[3], new[3], notes)
    if kind in ("array", "dynamic_array"):
        # Elements sit one after another: an element may change only in place.
        if old[2][1] != new[2][1] or new[1] < old[1] or not _fit(old[2], new[2], notes):
            return False
        if new[1] > old[1]:
            notes.add("grown")
        return True
    if kind == "struct":
        if len(new[2]) < len(old[2]):
            return False
        # A member that keeps its name keeps its place; else a swap of two
        # members of one type would pass as two renames.
        was_at = {label: at for label, *at, _ in old[2]}
        if any(was_at.get(label, at) != at for label, *at, _ in new[2]):
            return False
        for (old_label, *old_at, old_shape), (new_label, *new_at, new_shape) in zip(old[2], new[2]):
            if old_at != new_at or not _fit(old_shape, new_shape, notes):
                return False
            if old_label != new_label:
                notes.add("renamed")
        if new[1] != old[1] or len(new[2]) > len(old[2]):
            notes.add("grown")
        return True
    return False


def _declarations(layouts: dict) -> list[Decl]:
    """Every state variable the layouts (source_id -> solc layout, None for none)
    declare, sorted by position."""
    out = []
    for source_id, layout in layouts.items():
        if layout is None:
            continue
        types = layout.get("types") or {}
        for var in layout.get("storage") or []:
            kind = types[var["type"]]
            start = int(var["slot"]) * 32 + var["offset"]
            end = start + int(kind["numberOfBytes"])
            out.append(Decl(start, end, source_id, var["label"], kind["label"], _shape(types, var["type"])))
    return sorted(out, key=lambda d: (d.start, d.end, d.source))


def _check_storage(proxy: str, resolved: dict, declared: list[Decl], findings: Findings) -> str:
    """Every facet runs against the proxy's one storage, so storage bytes that two
    facets both declare must hold the same variable (name and type) in both.

    Sees declared state variables and annotated ERC-7201 structs: fixed-slot
    assembly and `.evm` facets are invisible to it."""
    unseen = [source_id for source_id, (_, _, layout) in resolved.items() if layout is None]

    shared = set()  # slots declared identically by more than one facet
    for i, a in enumerate(declared):
        for b in declared[i + 1 :]:
            if b.start >= a.end:
                break  # sorted by first byte: nothing later overlaps `a`
            if a.source == b.source:
                continue
            if (a.start, a.end, a.label, a.shape) == (b.start, b.end, b.label, b.shape):
                shared.add(a.start // 32)
                continue
            if a.label.startswith("erc7201:") and (a.start, a.end, a.label) == (b.start, b.end, b.label) and _fit(a.shape, b.shape, set()) and _fit(b.shape, a.shape, set()):
                # One namespace defined twice, as OZ's `_owner` and a local `owner`: same bytes.
                findings.warn(f"{proxy} {a.label}: {a.source} and {b.source} name its members differently")
                continue
            findings.fail(
                f"{proxy} storage {_position(b.start)}: {b.source} declares {b.type} {b.label} "
                f"over {a.source}'s {a.type} {a.label} at {_position(a.start)}"
            )

    checked = len(resolved) - len(unseen)
    namespaces = len({d.label for d in declared if d.label.startswith("erc7201:")})
    line = f"  storage      {_plural(checked, 'facet')} checked, {_plural(len(shared), 'slot')} shared"
    if namespaces:
        line += f", {_plural(namespaces, 'ERC-7201 namespace')}"
    if unseen:
        line += f"; not visible: {', '.join(unseen)}"
    return line


def _compare_storage(proxy: str, label: str, old: list[Decl], new: list[Decl], findings: Findings) -> str:
    """Storage the facets at `label` declared must read back the same at HEAD:
    same position, same shape. A struct or fixed array may grow where its new
    bytes overlap nothing declared before; a rename or a dropped variable warns."""
    old = list({(d.start, d.end, d.label, d.shape): d for d in reversed(old)}.values())[::-1]
    new = list({(d.start, d.end, d.label, d.shape): d for d in reversed(new)}.values())[::-1]
    grown = renamed = dropped = 0
    where: dict = {}  # label -> starts; independent facets may reuse a name
    for d in new:
        where.setdefault(d.label, set()).add(d.start)
    for o in old:
        was = f"{o.type} {o.label} ({o.source} at {label})"
        if o.label in where and o.start not in where[o.label]:
            # Otherwise a variable inserted before it reads as its rename.
            findings.fail(f"{proxy} storage {_position(o.start)}: {was} moves to {_position(min(where[o.label]))}")
        overlapping = [n for n in new if n.start < o.end and o.start < n.end]
        if not overlapping:
            dropped += 1
            findings.warn(f"{proxy} storage {_position(o.start)}: nothing declares {was} any more; its data stays")
        for n in overlapping:
            notes: set = set()
            if n.start != o.start or not _fit(o.shape, n.shape, notes):
                findings.fail(f"{proxy} storage {_position(n.start)}: {n.source} declares {n.type} {n.label} over {was}")
                continue
            if (n.label != o.label and o.label not in where) or "renamed" in notes:
                renamed += 1
                findings.warn(f"{proxy} storage {_position(n.start)}: {was} is renamed in {n.type} {n.label}")
            if "grown" in notes or n.end > o.end:
                grown += 1
    line = f"  layout       vs {label}: {_plural(len(old), 'variable')} checked"
    extras = [f"{count} {word}" for count, word in ((grown, "grown"), (renamed, "renamed"), (dropped, "dropped")) if count]
    return line + (f", {', '.join(extras)}" if extras else "")


class Baselines:
    """The storage recorded deployments declared, read from their commits checked
    out without a build: solc's analysis alone yields the layout."""

    def __init__(self, root: pathlib.Path, solc_version: str | None):
        self.root = root
        self.solc_version = solc_version
        self._trees: SourceTrees | None = None
        # (commit, sources) -> declarations; namespaces depend on the whole set's imports
        self._declared: dict = {}

    def declarations(self, commit: str, source_ids: frozenset) -> list[Decl]:
        if (commit, source_ids) not in self._declared:
            try:
                if self._trees is None:
                    self._trees = SourceTrees(self.root, build=False)
                tree = self._trees.get(commit)
            except click.ClickException as e:
                raise click.ClickException(
                    f"cannot check out {commit[:7]} (a shallow clone needs it fetched): {e.message}"
                )
            layouts, namespaces = storage_layouts(tree, [facet_from_source_id(s) for s in source_ids], self.solc_version)
            self._declared[(commit, source_ids)] = _declarations(layouts | namespaces)
        return self._declared[(commit, source_ids)]

    def close(self) -> None:
        if self._trees is not None:
            self._trees.close()


def _recorded_states(history: dict) -> list[tuple[str, str, frozenset]]:
    """(name, gitCommit, Solidity sources) of every deployment the proxy has run,
    whose storage is still there: `legacy`, the implementation before ERC-8167,
    then `current`, `proposed`, and retired facets grouped by commit."""
    states = []
    if legacy := history.get("legacy"):
        states.append(("legacy", legacy["gitCommit"], frozenset([legacy["source"]])))
    for name in ("current", "proposed"):
        if state := history.get(name):
            states.append((name, state["gitCommit"], frozenset(state["facets"])))
    retired: dict = {}
    for record in history.get("history", {}).values():
        if "source" in record:  # not a generated `selectors()` delegate
            retired.setdefault(record["gitCommit"], set()).add(record["source"])
    states.extend(("history", commit, frozenset(sources)) for commit, sources in retired.items())
    return [(name, commit, frozenset(s for s in sources if not s.endswith(".evm"))) for name, commit, sources in states]


def _check_storage_history(proxy, states, head, baselines, findings) -> list[str]:
    """Compare HEAD's storage with each recorded state's. Unchanged bytecode
    doesn't mean unchanged storage: a facet that never touches a variable
    compiles the same whatever type it declares there."""
    lines = []
    seen = set()
    for name, commit, recorded in states:
        if not recorded or (commit, recorded) in seen:
            continue
        seen.add((commit, recorded))
        label = f"{name} {commit[:7]}"
        try:
            old = baselines.declarations(commit, recorded)
        except click.ClickException as e:
            findings.fail(f"{proxy}: cannot read the storage at {label}: {e.message}")
            lines.append(f"  layout       vs {label}: not readable")
            continue
        lines.append(_compare_storage(proxy, label, old, head, findings))
    return lines


def _initcode_hash(facet, root, recorded: dict, proxy: str, findings: Findings) -> tuple[str | None, list[str]]:
    """(keccak of the creation code `deploy` would send, constructor args it
    would prompt for). The hash is None when args are missing, or on a failure."""
    source_id = facet.source_id
    args = recorded.get("constructorArgs") or {}
    try:
        if facet.kind == "sol":
            missing = [arg["name"] for arg in constructor_inputs(facet, root) if arg["name"] not in args]
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


def _check_proxy(entry, chain, resolved, head, storage_line, root, run_deployed, baselines, findings) -> list[str]:
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
    lines.extend(
        _check_storage_history(proxy, _recorded_states(history), head, baselines, findings)
    )

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

    try:
        run(["forge", "build"], root)
        lines.append(f"build  {_build_context(root)}")
    except click.ClickException as e:
        findings.fail(f"build: {e.message}")
        return lines, findings

    solc_version = _built_solc_version(root)
    resolved = [_resolve(entry, root, solc_version, findings) for entry in ledger]
    storage_lines = [
        _check_storage(to_checksum_address(entry["address"]), *r, findings) if r is not None else None
        for entry, r in zip(ledger, resolved)
    ]

    recorded_chains = {c for entry in ledger for c in entry.get("deployments", {})}
    chains = [chain] if chain else sorted(recorded_chains, key=int) or [None]
    baselines = Baselines(root, solc_version)
    try:
        for c in chains:
            run_deployed: dict = {}  # initcodeHash -> record, shared across proxies as `deploy` does
            for entry, r, storage_line in zip(ledger, resolved, storage_lines):
                if r is None:
                    lines.extend(["", f"{to_checksum_address(entry['address'])}  ·  facetSrc does not resolve"])
                    continue
                lines.extend(_check_proxy(entry, c, *r, storage_line, root, run_deployed, baselines, findings))
    finally:
        baselines.close()
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
    if findings.warnings:
        out.append(f"WARNING  {_plural(len(findings.warnings), 'warning')}")
        out.extend(f"  - {message}" for message in findings.warnings)
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
