"""Storage layouts of Solidity facets, read from solc without generating code.

A layout depends only on the sources, not on the optimizer, via-IR or the EVM
version, so solc is asked for `storageLayout` alone. It then stops after analysis:
under a second where a via-IR build of the same sources takes minutes. That makes
a layout cheap for HEAD and for any recorded commit checked out without a build.
"""

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


def solc_binary(root: pathlib.Path, fallback_version: str | None = None) -> str:
    """The solc `forge` would compile `root` with: foundry.toml's `solc` (a
    version or a path), else `fallback_version`."""
    configured = get_forge_config(root).get("solc")
    version = configured or fallback_version
    if not version:
        raise click.ClickException(
            f"{root}: no `solc` in foundry.toml and no build to take the compiler version from"
        )
    version = version.split("+", 1)[0]
    if not _VERSION.match(version):
        return str(root / version)  # a path; joining keeps an absolute one as it is
    for svm in _svm_dirs():
        binary = svm / version / f"solc-{version}"
        if binary.exists():
            return str(binary)
    raise click.ClickException(f"solc {version} is not installed; a `forge build` that uses it installs it")


def compiler_version(artifact: pathlib.Path) -> str | None:
    """The solc version a forge artifact was built with."""
    try:
        return json.loads(artifact.read_text())["metadata"]["compiler"]["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


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


def _compile(root: pathlib.Path, sources: dict, selection: dict, fallback_version: str | None) -> dict:
    """solc's analysis of `sources`: ASTs, and the storage layouts `selection` asks for."""
    standard_json = {
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "remappings": run(["forge", "remappings"], root).split(),
            # The AST holds UDVTs' underlying types.
            "outputSelection": {"*": {"": ["ast"]}, **selection},
        },
    }
    solc = solc_binary(root, fallback_version)
    out = json.loads(
        run([solc, "--standard-json", "--base-path", ".", "--allow-paths", "."], root, stdin=json.dumps(standard_json))
    )
    errors = [e.get("formattedMessage", e.get("message", "")).strip() for e in out.get("errors", []) if e.get("severity") == "error"]
    if errors:
        raise click.ClickException("solc: " + "\n".join(errors))
    return out


def _layout(out: dict, path: str, contract: str) -> dict | None:
    """`contract`'s storage layout from solc output `out`. A user-defined value
    type's entry gains `underlying`: solc labels it by name only, so
    `type Amount is uint256` and `is int256` look alike."""
    found = out.get("contracts", {}).get(path, {}).get(contract)
    if found is None:
        return None
    asts = [source.get("ast") for source in out.get("sources", {}).values()]
    underlying = {n["id"]: n["underlyingType"]["typeDescriptions"]["typeString"] for n in _walk(asts, "UserDefinedValueTypeDefinition")}
    layout = found["storageLayout"]
    for type_id, t in (layout.get("types") or {}).items():
        if (match := _UDVT.match(type_id)) and int(match[1]) in underlying:
            t["underlying"] = underlying[int(match[1])]
    return layout


def erc7201_slot(namespace: str) -> int:
    """ERC-7201: keccak256(abi.encode(uint256(keccak256(id)) - 1)) & ~0xff."""
    inner = int.from_bytes(keccak(text=namespace), "big") - 1
    return int.from_bytes(keccak(inner.to_bytes(32, "big")), "big") & ~0xFF


_NAMESPACE = re.compile(r"@custom:storage-location\s+erc7201:(\S+)")
_SYNTHETIC = "josuke-erc7201.sol"


def _namespaces(root: pathlib.Path, out: dict, fallback_version: str | None) -> dict:
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
    out = _compile(root, {_SYNTHETIC: {"content": synthetic}}, {_SYNTHETIC: {"JosukeERC7201": ["storageLayout"]}}, fallback_version)
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


def storage_layouts(root: pathlib.Path, facets: list, fallback_version: str | None = None) -> tuple[dict, dict]:
    """(source_id -> solc's storage layout for the Solidity facets among `facets`,
    "<path>:<Struct>" -> layout of each ERC-7201 namespace their sources define),
    as the sources in `root` declare them.

    A namespace counts as declared when any facet's imports define it, used or not.
    Its annotation is trusted: the slot code actually reads is not checked."""
    wanted = [facet for facet in facets if facet.kind == "sol"]
    if not wanted:
        return {}, {}
    selection: dict = {}
    for facet in wanted:
        selection.setdefault(facet.path, {})[facet.contract] = ["storageLayout"]
    out = _compile(root, {path: {"urls": [path]} for path in selection}, selection, fallback_version)

    layouts = {}
    for facet in wanted:
        layout = _layout(out, facet.path, facet.contract)
        if layout is None:
            raise click.ClickException(f"{facet.source_id}: no such contract")
        layouts[facet.source_id] = layout
    return layouts, _namespaces(root, out, fallback_version)
