import json

import pytest
from click.testing import CliRunner
from eth_utils import to_checksum_address

from josuke.cli import main

ADDRESS = "0x2222222222222222222222222222222222222222"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return CliRunner()


def read_ledger(tmp_path):
    return json.loads((tmp_path / "josuke.json").read_text())


def test_init_creates_empty_ledger(runner, tmp_path):
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0
    assert read_ledger(tmp_path) == []


def test_init_refuses_to_clobber(runner, tmp_path):
    (tmp_path / "josuke.json").write_text("[]\n")
    result = runner.invoke(main, ["init"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_add_registers_new_proxy(runner, tmp_path):
    runner.invoke(main, ["init"])
    result = runner.invoke(
        main,
        ["add", ADDRESS, "src/facets/*.sol", "lib/erc8169/implementation.evm"],
    )
    assert result.exit_code == 0, result.output
    assert read_ledger(tmp_path) == [
        {
            "address": ADDRESS,
            "facetSrc": ["src/facets/*.sol", "lib/erc8169/implementation.evm"],
        }
    ]


def test_add_appends_to_existing_proxy(runner, tmp_path):
    runner.invoke(main, ["init"])
    runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol"])
    result = runner.invoke(
        main, ["add", ADDRESS.lower(), "lib/erc8169/implementation.evm"]
    )
    assert result.exit_code == 0, result.output
    assert read_ledger(tmp_path)[0]["facetSrc"] == [
        "src/facets/*.sol",
        "lib/erc8169/implementation.evm",
    ]
    assert len(read_ledger(tmp_path)) == 1


def test_add_checksums_address(runner, tmp_path):
    runner.invoke(main, ["init"])
    lower = "0x" + "ab" * 20
    result = runner.invoke(main, ["add", lower, "src/facets/*.sol"])
    assert result.exit_code == 0, result.output
    stored = read_ledger(tmp_path)[0]["address"]
    assert stored == to_checksum_address(lower)
    assert stored != lower


def test_add_does_not_set_deployment_fields(runner, tmp_path):
    runner.invoke(main, ["init"])
    runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol"])
    runner.invoke(main, ["add", ADDRESS, "lib/erc8169/implementation.evm"])
    assert set(read_ledger(tmp_path)[0]) == {"address", "facetSrc"}


def test_add_rejects_bad_address(runner):
    runner.invoke(main, ["init"])
    result = runner.invoke(main, ["add", "0xdeadbeef", "src/facets/*.sol"])
    assert result.exit_code != 0
    assert "invalid address" in result.output


def test_add_requires_a_facet(runner):
    runner.invoke(main, ["init"])
    result = runner.invoke(main, ["add", ADDRESS])
    assert result.exit_code != 0


def test_add_rejects_duplicate_in_args(runner):
    runner.invoke(main, ["init"])
    result = runner.invoke(
        main, ["add", ADDRESS, "src/facets/*.sol", "src/facets/*.sol"]
    )
    assert result.exit_code != 0
    assert "duplicate" in result.output


def test_add_rejects_facet_already_on_proxy(runner):
    runner.invoke(main, ["init"])
    runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol"])
    result = runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol"])
    assert result.exit_code != 0
    assert "already has" in result.output


def test_add_rejected_duplicate_does_not_write(runner, tmp_path):
    runner.invoke(main, ["init"])
    runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol"])
    runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol", "new.evm"])
    assert read_ledger(tmp_path)[0]["facetSrc"] == ["src/facets/*.sol"]


def test_add_without_ledger_errors(runner):
    result = runner.invoke(main, ["add", ADDRESS, "src/facets/*.sol"])
    assert result.exit_code != 0
    assert "josuke init" in result.output
