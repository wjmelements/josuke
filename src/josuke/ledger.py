import json
from functools import cache
from importlib.resources import files

import click
from eth_utils import is_hex_address, to_checksum_address
from jsonschema import Draft202012Validator

DEFAULT_LEDGER = "josuke.json"


def parse_address(address: str) -> str:
    if not is_hex_address(address):
        raise click.ClickException(f"invalid address: {address}")
    return to_checksum_address(address)


def load_ledger(ledger_path) -> list:
    try:
        return json.loads(ledger_path.read_text())
    except FileNotFoundError:
        raise click.ClickException(f"{ledger_path} not found; run `josuke init` first")
    except json.JSONDecodeError as e:
        raise click.ClickException(f"{ledger_path} is not valid JSON: {e}")


def write_ledger(ledger_path, ledger) -> None:
    ledger_path.write_text(json.dumps(ledger, indent=4) + "\n")


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(files(__package__).joinpath("josuke.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_ledger(ledger) -> list[str]:
    """Every way `ledger` departs from josuke.schema.json, one line each, located
    by JSON path ("$[0].deployments.314.current.gitCommit: ...")."""
    errors = []
    for error in sorted(_validator().iter_errors(ledger), key=lambda e: list(e.absolute_path)):
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path)
        errors.append(f"${path}: {error.message}")
    return errors
