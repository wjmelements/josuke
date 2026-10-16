"""Layout extraction must resolve the same sources and compilers as Foundry."""

import shutil

import click
import pytest

from josuke import layout
from josuke.facet import facet_from_source_id
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


TSTORE = 'pragma solidity 0.8.28; contract A { uint256 internal a; function f() external { assembly { tstore(0, 1) } } }'


def test_layout_analyses_with_the_configured_evm_version(tmp_path):
    # solc 0.8.28 defaults to cancun; Foundry builds for shanghai, which has no tstore.
    (tmp_path / "src").mkdir()
    (tmp_path / "foundry.toml").write_text('[profile.default]\nsolc="0.8.28"\nevm_version="shanghai"\n')
    (tmp_path / "src/A.sol").write_text(TSTORE)
    with pytest.raises(click.ClickException, match="Cancun-compatible"):
        storage_layouts(tmp_path, [facet_from_source_id("src/A.sol:A")])


def test_evm_version_newer_than_solc_falls_back_like_foundry(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "foundry.toml").write_text('[profile.default]\nsolc="0.8.28"\nevm_version="osaka"\n')
    (tmp_path / "src/A.sol").write_text(TSTORE)
    run(["forge", "build"], tmp_path)
    layouts, _ = storage_layouts(tmp_path, [facet_from_source_id("src/A.sol:A")])
    assert layouts["src/A.sol:A"]["storage"][0]["label"] == "a"


@pytest.fixture
def empty_svm(tmp_path, monkeypatch):
    """A HOME whose svm cache starts empty, leaving the real one untouched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    layout._install_solc.cache_clear()
    yield home
    layout._install_solc.cache_clear()


def _pinned_project(path, version="0.8.26"):
    (path / "src").mkdir(parents=True)
    (path / "foundry.toml").write_text(f'[profile.default]\nsolc="{version}"\n')
    (path / "src/A.sol").write_text(f'pragma solidity {version}; contract A {{ uint256 internal data; }}')
    return path


def test_missing_compiler_is_installed(tmp_path, empty_svm):
    project = _pinned_project(tmp_path / "project")
    layouts, _ = storage_layouts(project, [facet_from_source_id("src/A.sol:A")])
    assert layouts["src/A.sol:A"]["storage"][0]["label"] == "data"
    assert layout._installed_solc("0.8.26").is_relative_to(empty_svm)


def test_missing_compiler_is_installed_once(tmp_path, empty_svm, monkeypatch):
    calls = []
    real_run = layout.run
    monkeypatch.setattr(layout, "run", lambda cmd, *a, **k: calls.append(cmd) or real_run(cmd, *a, **k))
    for name in ("one", "two"):
        storage_layouts(_pinned_project(tmp_path / name), [facet_from_source_id("src/A.sol:A")])
    assert sum(cmd[:2] == ["forge", "build"] for cmd in calls) == 1


def test_compiler_path_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "_install_solc", lambda version: pytest.fail("installed a path"))
    assert layout.solc_binary(tmp_path, {"solc": "bin/solc"}) == str(tmp_path / "bin/solc")


def test_install_failure_names_the_version(tmp_path, empty_svm, monkeypatch):
    def fail(cmd, *args, **kwargs):
        raise click.ClickException("forge build --use failed:\nno network")
    monkeypatch.setattr(layout, "run", fail)
    with pytest.raises(click.ClickException, match=r"(?s)installing solc 0\.8\.26 .*no network"):
        layout.solc_binary(tmp_path, {"solc": "0.8.26"})
