import json

import click
from eth_utils import is_hex_address, to_checksum_address

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
