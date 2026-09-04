"""Tests for josuke.evm's .evm-facet artifact build."""

import json
import shutil
import textwrap

import click
import pytest

from josuke import evm
from josuke.evm import _governing_makefile, evm_artifact

ARTIFACT = {
    "bytecode": {"object": "0xdeadbeef"},
    "abi": [{"type": "function", "name": "impl", "inputs": [], "outputs": []}],
}


def test_governing_makefile_picks_nearest_ancestor(tmp_path):
    (tmp_path / "Makefile").write_text("")
    sub = tmp_path / "lib" / "dep"
    (sub / "src").mkdir(parents=True)
    (sub / "Makefile").write_text("")
    assert _governing_makefile(sub / "src" / "X.evm", tmp_path) == sub


def test_governing_makefile_none_above_root(tmp_path):
    (tmp_path / "Makefile").write_text("")  # this is the parent of root
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    assert _governing_makefile(root / "src" / "X.evm", root) is None


def _stub_makefile(directory, stem):
    """A Makefile whose artifact rule just writes the canned ARTIFACT json."""
    (directory / "Makefile").write_text(
        textwrap.dedent(f"""\
        out/{stem}.evm/{stem}.json:
        \tmkdir -p out/{stem}.evm
        \tprintf '%s' '{json.dumps(ARTIFACT)}' > $@
        """)
    )


needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="requires make")


@needs_make
def test_evm_artifact_builds_and_reads(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    _stub_makefile(tmp_path, "Impl")

    got = evm_artifact(src / "Impl.evm", tmp_path)
    assert got == {"initcode": "deadbeef", "abi": ARTIFACT["abi"]}


@needs_make
def test_evm_artifact_memoises_the_build(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    _stub_makefile(tmp_path, "Impl")

    evm_artifact(src / "Impl.evm", tmp_path)
    artifact = tmp_path / "out" / "Impl.evm" / "Impl.json"
    artifact.write_text(json.dumps({**ARTIFACT, "bytecode": {"object": "0xcafe"}}))

    # second call must not re-run make (which would overwrite our edit back)
    assert evm_artifact(src / "Impl.evm", tmp_path)["initcode"] == "cafe"


def test_evm_artifact_without_makefile_raises(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    with pytest.raises(click.ClickException, match="no Makefile"):
        evm_artifact(src / "Impl.evm", tmp_path)


@needs_make
def test_evm_artifact_without_abi_raises(tmp_path):
    evm._built.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "Impl.evm").write_text("// asm\n")
    (tmp_path / "Makefile").write_text(
        textwrap.dedent("""\
        out/Impl.evm/Impl.json:
        \tmkdir -p out/Impl.evm
        \tprintf '%s' '{"bytecode":{"object":"0x00"},"deployedBytecode":{"object":"0x00"}}' > $@
        """)
    )
    with pytest.raises(click.ClickException, match="no ABI"):
        evm_artifact(src / "Impl.evm", tmp_path)
