import json
import pathlib
import subprocess

import click

from .proc import run

# artifact paths already built this process, so repeated facet_abi / facet_initcode
# calls don't re-invoke make (make no-ops when up to date, but this also keeps its
# output off the console).
_built: set[str] = set()


def execute(initcode_hex: str, sender: str | None = None) -> str:
    """Run `evm -x`: execute creation bytecode, return the deployed runtime hex.

    `sender` sets `msg.sender` for the constructor, so an immutable derived from
    the deployer is reproduced.
    """
    if sender is None:
        stdin = initcode_hex
    else:
        stdin = json.dumps({"from": sender, "data": initcode_hex})
    return subprocess.run(
        ["evm", "-x"],
        input=stdin,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _governing_makefile(source: pathlib.Path, root: pathlib.Path) -> pathlib.Path | None:
    """Nearest ancestor of `source` (up to and including `root`) with a Makefile."""
    root = root.resolve()
    directory = source.resolve().parent
    while True:
        if (directory / "Makefile").exists() or (directory / "GNUmakefile").exists():
            return directory
        if directory == root or root not in directory.parents:
            return None
        directory = directory.parent


def evm_artifact(source: pathlib.Path, root: pathlib.Path) -> dict:
    """Assemble the `.evm` facet at `source` via its Makefile and return
    `{"initcode": <hex>, "abi": [...]}`.

    ERC-8167 `.evm` files are evm-assembler source, not raw bytecode. Projects that
    ship them attach a Makefile rule (the `ASM_ARTIFACT` macro) that assembles the
    source and merges in the matching interface ABI, writing
    `out/<stem>.evm/<stem>.json`. josuke locates the Makefile that governs the
    source's directory (a vendored submodule has its own) and builds that artifact
    with `make -C`. The artifact is the only way to get the facet's ABI."""
    stem = source.stem
    makedir = _governing_makefile(source, root)
    if makedir is None:
        raise click.ClickException(
            f"{source}: no Makefile governs this .evm source; one must build "
            f"out/{stem}.evm/{stem}.json (with an attached ABI)"
        )

    rel = f"out/{stem}.evm/{stem}.json"
    artifact = makedir / rel
    key = str(artifact.resolve())
    if key not in _built:
        run(["make", "-C", str(makedir), rel])
        _built.add(key)

    if not artifact.exists():
        raise click.ClickException(
            f"{source}: `make -C {makedir} {rel}` did not produce {artifact}"
        )

    data = json.loads(artifact.read_text())
    abi = data.get("abi")
    if not abi:
        raise click.ClickException(
            f"{artifact}: no ABI attached; the Makefile rule must merge in the "
            f"matching interface ABI so josuke can read the facet's selectors"
        )
    return {
        "initcode": data["bytecode"]["object"].removeprefix("0x"),
        "abi": abi,
    }
