"""Ethereum JSON-RPC access: the raw `rpc` call plus thin `eth_*` wrappers."""

import subprocess
from os import environ

import click
import requests


def rpc(method: str, params: list):
    resp = requests.post(
        environ["ETH_RPC_URL"],
        json={"id": 1, "jsonrpc": "2.0", "method": method, "params": params},
    )
    if resp.status_code != 200:
        raise click.ClickException(f"{method}: HTTP {resp.status_code}")
    body = resp.json()
    if body.get("error"):
        raise click.ClickException(f"{method}: {body['error']}")
    return body["result"]


def eth_get_code(address: str, block: str = "latest") -> str:
    """The 0x-prefixed runtime bytecode at `address`."""
    return rpc("eth_getCode", [address, block])


def chain_id() -> str:
    """Decimal chain id string, from `cast` when available, else `eth_chainId`."""
    try:
        out = subprocess.run(
            ["cast", "chain-id"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return str(int(out))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return str(int(rpc("eth_chainId", []), 16))
