"""Storage layouts of Solidity facets, read from solc without generating code.

A layout depends only on the sources, not on the optimizer, via-IR or the EVM
version, so solc is asked for `storageLayout` alone. It then stops after analysis:
under a second where a via-IR build of the same sources takes minutes. That makes
a layout cheap for HEAD and for any recorded commit checked out without a build.
"""

import functools
import json
import os
import pathlib
import posixpath
import re

import click
from eth_utils import keccak

from .forge import get_forge_config
from .proc import run

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_ENUM = re.compile(r"^t_enum\(.*\)(\d+)$")
_UDVT = re.compile(r"^t_userDefinedValueType\(.*\)(\d+)$")


def _svm_dirs() -> list[pathlib.Path]:
    """Where foundry's svm keeps the solc releases it installs."""
    home = pathlib.Path.home()
    xdg = os.environ.get("XDG_DATA_HOME")
    return [
        home / ".svm",
        *([pathlib.Path(xdg) / "svm"] if xdg else []),
        home / ".local" / "share" / "svm",
        home / "Library" / "Application Support" / "svm",
    ]


def solc_binary(root: pathlib.Path, config: dict, fallback_version: str | None = None) -> str:
    """Locate the configured compiler or a version resolved from this checkout."""
    version = config.get("solc") or fallback_version
    if not version:
        raise click.ClickException(
            f"{root}: no configured or resolved Solidity compiler"
        )
    version = version.split("+", 1)[0]
    if not _VERSION.match(version):
        return str(root / version)  # a path; joining keeps an absolute one as it is
    for svm in _svm_dirs():
        binary = svm / version / f"solc-{version}"
        if binary.exists():
            return str(binary)
    raise click.ClickException(f"solc {version} is not installed; a `forge build` that uses it installs it")


def _walk(node, kind: str):
    """Every AST node of `kind` under `node`."""
    if isinstance(node, dict):
        if node.get("nodeType") == kind:
            yield node
        for child in node.values():
            yield from _walk(child, kind)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child, kind)


def _errors(out: dict) -> list[str]:
    return [e.get("formattedMessage", e.get("message", "")).strip() for e in out.get("errors", []) if e.get("severity") == "error"]


def _compile(root: pathlib.Path, config: dict, sources: dict, selection: dict, fallback_version: str | None) -> dict:
    """solc's analysis of `sources`: ASTs, and the storage layouts `selection` asks for."""
    settings = {
        "remappings": config.get("remappings") or [],
        # The AST holds UDVTs' underlying types.
        "outputSelection": {"*": {"": ["ast"]}, **selection},
    }
    if config.get("evm_version"):
        # Analysis checks inline assembly opcodes against the EVM version.
        settings["evmVersion"] = config["evm_version"]
    standard_json = {"language": "Solidity", "sources": sources, "settings": settings}
    solc = solc_binary(root, config, fallback_version)
    includes = config.get("include_paths") or []
    allowed = [".", *(config.get("allow_paths") or []), *(config.get("libs") or []), *includes]
    command = [solc, "--standard-json", "--base-path", ".", "--allow-paths", ",".join(allowed)]
    for path in includes:
        command.extend(["--include-path", path])
    out = json.loads(run(command, root, stdin=json.dumps(standard_json)))
    if "evmVersion" in settings and any("Invalid EVM version" in error for error in _errors(out)):
        # Foundry lowers an EVM version this solc predates to the newest it knows,
        # which is solc's default.
        del settings["evmVersion"]
        out = json.loads(run(command, root, stdin=json.dumps(standard_json)))
    errors = _errors(out)
    if errors:
        raise click.ClickException("solc: " + "\n".join(errors))
    return out


def _layout(out: dict, path: str, contract: str) -> dict | None:
    """Enrich solc's layout with UDVT underlying types and ordered enum members."""
    found = out.get("contracts", {}).get(path, {}).get(contract)
    if found is None:
        return None
    asts = [source.get("ast") for source in out.get("sources", {}).values()]
    underlying = {n["id"]: n["underlyingType"]["typeDescriptions"]["typeString"] for n in _walk(asts, "UserDefinedValueTypeDefinition")}
    enums = {n["id"]: [m["name"] for m in n["members"]] for n in _walk(asts, "EnumDefinition")}
    layout = found["storageLayout"]
    for type_id, t in (layout.get("types") or {}).items():
        if (match := _UDVT.match(type_id)) and int(match[1]) in underlying:
            t["underlying"] = underlying[int(match[1])]
        if (match := _ENUM.match(type_id)) and int(match[1]) in enums:
            t["enumMembers"] = enums[int(match[1])]
    return layout


def erc7201_slot(namespace: str) -> int:
    """ERC-7201: keccak256(abi.encode(uint256(keccak256(id)) - 1)) & ~0xff."""
    inner = int.from_bytes(keccak(text=namespace), "big") - 1
    return int.from_bytes(keccak(inner.to_bytes(32, "big")), "big") & ~0xFF


_NAMESPACE = re.compile(r"@custom:storage-location\s+erc7201:(\S+)")
_SYNTHETIC = "josuke-erc7201.sol"


def _namespaces(root: pathlib.Path, config: dict, out: dict, fallback_version: str | None) -> dict:
    """"<path>:<Struct>" -> a layout declaring that ERC-7201 struct at its slot,
    for every annotated struct in `out`'s sources. solc lays the structs out: a
    second analysis declares one state variable per struct."""
    found = []  # (path, canonical name, namespace id)
    for path, source in out.get("sources", {}).items():
        for struct in _walk(source.get("ast"), "StructDefinition"):
            if match := _NAMESPACE.search((struct.get("documentation") or {}).get("text", "")):
                found.append((path, struct["canonicalName"], match[1]))
    if not found:
        return {}

    imports = "".join(f'import "{path}" as N{i};\n' for i, (path, _, _) in enumerate(found))
    variables = "".join(f"    N{i}.{name} ns{i};\n" for i, (_, name, _) in enumerate(found))
    synthetic = f"// SPDX-License-Identifier: UNLICENSED\n{imports}contract JosukeERC7201 {{\n{variables}}}\n"
    out = _compile(root, config, {_SYNTHETIC: {"content": synthetic}}, {_SYNTHETIC: {"JosukeERC7201": ["storageLayout"]}}, fallback_version)
    layout = _layout(out, _SYNTHETIC, "JosukeERC7201")

    # A remapping may send a synthetic import elsewhere than the facets' own.
    struct_paths = {n["id"]: path for path, source in out["sources"].items() for n in _walk(source.get("ast"), "StructDefinition")}
    declared = {v["name"]: v["typeName"].get("referencedDeclaration") for v in _walk(out["sources"][_SYNTHETIC]["ast"], "VariableDeclaration")}

    namespaces = {}
    for i, (path, name, namespace) in enumerate(found):
        # `./src/A.sol` and `src/A.sol` name one file.
        if posixpath.normpath(struct_paths.get(declared.get(f"ns{i}"), "")) != posixpath.normpath(path):
            raise click.ClickException(f"{path}:{name}: a synthetic import of it resolves to another file")
        var = next(v for v in layout["storage"] if v["label"] == f"ns{i}")
        namespaces[f"{path}:{name}"] = {
            "storage": [var | {"label": f"erc7201:{namespace}", "slot": str(erc7201_slot(namespace)), "offset": 0}],
            "types": layout["types"],
        }
    return namespaces


def _facet_layouts(root: pathlib.Path, config: dict, wanted: list, version: str | None) -> tuple[dict, dict]:
    selection: dict = {}
    for facet in wanted:
        selection.setdefault(facet.path, {})[facet.contract] = ["storageLayout"]
    out = _compile(root, config, {path: {"urls": [path]} for path in selection}, selection, version)
    layouts = {}
    for facet in wanted:
        layout = _layout(out, facet.path, facet.contract)
        if layout is None:
            raise click.ClickException(f"{facet.source_id}: no such contract")
        layouts[facet.source_id] = layout
    return layouts, _namespaces(root, config, out, version)


# Memoised per checkout, so every proxy in a ledger shares one resolution.
@functools.cache
def _resolved_versions(root: pathlib.Path) -> tuple[str, ...]:
    """Installed solc versions this checkout's pragmas resolve to, newest first,
    without a bytecode build."""
    resolved = json.loads(run(["env", "FOUNDRY_OFFLINE=true", "forge", "compiler", "resolve", "--json"], root))
    return tuple(sorted(
        [compiler["version"] for compiler in resolved.get("Solidity", [])],
        key=lambda version: tuple(map(int, version.split("+", 1)[0].split("."))),
        reverse=True,
    ))


def storage_layouts(root: pathlib.Path, facets: list) -> tuple[dict, dict]:
    """Facet and ERC-7201 layouts using this checkout's compiler configuration.

    A namespace counts as declared when any facet's imports define it, used or not.
    Its annotation is trusted: the slot code actually reads is not checked.
    """
    wanted = [facet for facet in facets if facet.kind == "sol"]
    if not wanted:
        return {}, {}
    config = get_forge_config(root)
    if config.get("solc"):
        return _facet_layouts(root, config, wanted, None)

    versions = _resolved_versions(root)
    if not versions:
        raise click.ClickException(f"{root}: no Solidity compiler resolved; pin solc in foundry.toml")
    if len(versions) == 1:
        return _facet_layouts(root, config, wanted, versions[0])

    # In a multi-version project each facet and its imports must compile together.
    layouts, namespaces = {}, {}
    for facet in wanted:
        errors = []
        for version in versions:
            try:
                found, annotated = _facet_layouts(root, config, [facet], version)
            except click.ClickException as error:
                errors.append(error.message)
                continue
            layouts.update(found)
            namespaces.update(annotated)
            break
        else:
            raise click.ClickException(f"{facet.source_id}: no resolved compiler can read its layout:\n" + "\n".join(errors))
    return layouts, namespaces
