"""Layout extraction must resolve the same sources and compilers as Foundry."""

import shutil

import pytest

from josuke.deploy import facet_from_source_id
from josuke.layout import storage_layouts
from josuke.proc import run

pytestmark = pytest.mark.skipif(shutil.which("forge") is None, reason="requires forge")


def test_layout_uses_foundry_include_paths(tmp_path):
    project = tmp_path / "contracts"
    (project / "src").mkdir(parents=True)
    (tmp_path / "common").mkdir()
    (tmp_path / "common/Shared.sol").write_text('pragma solidity 0.8.28; struct Shared { uint256 value; }')
    (project / "foundry.toml").write_text('[profile.default]\nsolc="0.8.28"\ninclude_paths=["../common"]\n[profile.default.lint]\nlint_on_build=false\n')
    (project / "src/A.sol").write_text('pragma solidity 0.8.28; import "Shared.sol"; contract A { Shared internal data; }')
    run(["forge", "build"], project)
    layouts, _ = storage_layouts(project, [facet_from_source_id("src/A.sol:A")])
    assert layouts["src/A.sol:A"]["storage"][0]["label"] == "data"


def test_unpinned_layout_resolves_its_own_compiler(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "foundry.toml").write_text('[profile.default]\n')
    (tmp_path / "src/A.sol").write_text('pragma solidity 0.8.28; contract A { uint256 internal data; }')
    run(["forge", "build"], tmp_path)
    layouts, _ = storage_layouts(tmp_path, [facet_from_source_id("src/A.sol:A")])
    assert layouts["src/A.sol:A"]["storage"][0]["label"] == "data"


def test_unpinned_project_can_use_multiple_compiler_versions(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "foundry.toml").write_text('[profile.default]\n')
    (tmp_path / "src/A.sol").write_text('pragma solidity 0.8.28; contract A { uint256 internal a; }')
    (tmp_path / "src/B.sol").write_text('pragma solidity 0.8.25; contract B { address internal b; }')
    run(["forge", "build"], tmp_path)
    layouts, _ = storage_layouts(tmp_path, [facet_from_source_id("src/A.sol:A"), facet_from_source_id("src/B.sol:B")])
    assert layouts["src/A.sol:A"]["storage"][0]["label"] == "a"
    assert layouts["src/B.sol:B"]["storage"][0]["label"] == "b"
