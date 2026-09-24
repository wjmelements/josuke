"""Ethereum JSON-RPC access: the raw `rpc` call plus thin `eth_*` wrappers."""

import subprocess
from os import environ

import click
import requests

from .trace import brief, span


class RpcError(click.ClickException):
    """The node answered with an HTTP failure or a JSON-RPC error."""


def rpc(method: str, params: list):
    with span(f"rpc {method} {brief(params)}"):
        resp = requests.post(
            environ["ETH_RPC_URL"],
            json={"id": 1, "jsonrpc": "2.0", "method": method, "params": params},
        )
    if resp.status_code != 200:
        raise RpcError(f"{method}: HTTP {resp.status_code}")
    body = resp.json()
    if body.get("error"):
        raise RpcError(f"{method}: {body['error']}")
    return body["result"]


def eth_get_code(address: str, block: str = "latest") -> str:
    """The 0x-prefixed runtime bytecode at `address`."""
    return rpc("eth_getCode", [address, block])


def eth_block_number() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def eth_get_logs(address: str, topics: list, from_block: int, to_block: int) -> list:
    """Logs emitted by `address` over the inclusive block range, matching `topics`."""
    return rpc(
        "eth_getLogs",
        [
            {
                "address": address,
                "topics": topics,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
            }
        ],
    )


def chain_id() -> str:
    """Decimal chain id string, from `cast` when available, else `eth_chainId`."""
    try:
        out = subprocess.run(
            ["cast", "chain-id"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return str(int(out))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return str(int(rpc("eth_chainId", []), 16))
