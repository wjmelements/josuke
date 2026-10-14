"""Facets: the deployable contracts `facetSrc` names, and their forge build artifacts."""

import functools
import json
import pathlib
from collections import namedtuple

import click

from .forge import get_forge_config


# A concrete, deployable facet resolved from a `facetSrc` pattern.
#   kind      "sol" | "evm"
#   path      repo-relative source path
#   contract  contract name (sol only; None for evm)
#   source_id the josuke.json key: "<path>:<Contract>" or "<path>.evm"
Facet = namedtuple("Facet", "kind path contract source_id")


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


# Memoised for the process: `deploy`, `verify` and `check` read each facet's ABI and
# bytecode from several checks. A key can't go stale within a run, since each commit is
# checked out at its own worktree and built before it is read.
@functools.cache
def facet_artifact(facet: Facet, root: pathlib.Path) -> dict:
    """Forge's build artifact for a Solidity facet, read from `out/` instead of a
    `forge inspect` per field. Forge nests an artifact whose file name another
    source shares, so the match is by the compilation target it records."""
    out_dir = root / get_forge_config(root).get("out", "out")
    name = pathlib.PurePosixPath(facet.path).name
    candidates = [out_dir / name / f"{facet.contract}.json", *sorted(out_dir.glob(f"**/{name}/{facet.contract}*.json"))]
    for path in dict.fromkeys(candidates):
        if not path.exists():
            continue
        artifact = json.loads(path.read_text())
        if artifact.get("metadata", {}).get("settings", {}).get("compilationTarget") == {facet.path: facet.contract}:
            return artifact
    raise click.ClickException(f"{facet.source_id}: no build artifact in {out_dir}")
