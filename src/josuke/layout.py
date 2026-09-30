"""Storage layouts of Solidity facets, read from solc without generating code.

A layout depends only on the sources, not on the optimizer, via-IR or the EVM
version, so solc is asked for `storageLayout` alone. It then stops after analysis:
under a second where a via-IR build of the same sources takes minutes. That makes
a layout cheap for HEAD and for any recorded commit checked out without a build.
"""

import json
import os
import pathlib
import re

import click

from .forge import get_forge_config
from .proc import run

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


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


def storage_layouts(root: pathlib.Path, facets: list, fallback_version: str | None = None) -> dict:
    """source_id -> solc's storage layout, for the Solidity facets among `facets`,
    as the sources in `root` declare them. One solc run covers them all."""
    wanted = [facet for facet in facets if facet.kind == "sol"]
    if not wanted:
        return {}
    selection: dict = {}
    for facet in wanted:
        selection.setdefault(facet.path, {})[facet.contract] = ["storageLayout"]
    standard_json = {
        "language": "Solidity",
        "sources": {path: {"urls": [path]} for path in selection},
        "settings": {
            "remappings": run(["forge", "remappings"], root).split(),
            "outputSelection": selection,
        },
    }
    solc = solc_binary(root, fallback_version)
    out = json.loads(
        run([solc, "--standard-json", "--base-path", ".", "--allow-paths", "."], root, stdin=json.dumps(standard_json))
    )
    errors = [e.get("formattedMessage", e.get("message", "")).strip() for e in out.get("errors", []) if e.get("severity") == "error"]
    if errors:
        raise click.ClickException("solc: " + "\n".join(errors))

    layouts = {}
    for facet in wanted:
        contract = out.get("contracts", {}).get(facet.path, {}).get(facet.contract)
        if contract is None:
            raise click.ClickException(f"{facet.source_id}: no such contract")
        layouts[facet.source_id] = contract["storageLayout"]
    return layouts
