from eth_abi import encode as abi_encode
from json import loads
from os import environ
from requests import post
from subprocess import run

from .evm import execute

class ContractSource:
    def __init__(self, path: str, name: str, config={}, root: str = ".", sender: str | None = None):
        self.path = path
        self.name = name
        self.config = config
        self.root = root
        self.sender = sender  # msg.sender

    def __repr__(self):
        return f"{self.path}:{self.name}"

    def __eq__(self, other) -> bool:
        return self.path == other.path and self.name == other.name


class UnconfiguredParameter(Exception):
    pass

source_map = {}

class Delegate:
    def __init__(self, address: str, source: ContractSource):
        self.address = address
        self.address20 = address.removeprefix("0x").lower()
        self.source = source
        self.deployed_bytecode = None
        source_map[address] = self

    def __repr__(self) -> str:
        return f"{self.source} (@{self.address})"

    def __eq__(self, other) -> bool:
        return self.address == other.address and self.source == other.source

    def fetch(self):
        result = post(environ['ETH_RPC_URL'], json={
            "id": 1,
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [self.address]
        })
        assert result.status_code == 200
        self.deployed_bytecode = loads(result.text)["result"].removeprefix("0x")

    def matches_source(self) -> bool:
        assert self.deployed_bytecode is not None

        source = str(self.source)
        root = self.source.root

        initcode = run(
            ["forge", "inspect", source, "bytecode"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip().removeprefix("0x")

        abi = loads(run(
            ["forge", "inspect", source, "abi", "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout)
        constructor = [method for method in abi if method["type"] == "constructor"]
        if constructor:
            abi_inputs = constructor[0]["inputs"]
            values_dict = self.source.config
            values = []
            for arg in abi_inputs:
                name = arg["name"]
                value = values_dict.get(name)
                if value is None:
                    raise UnconfiguredParameter(f"{name} is not configured for {source}")
                values.append(value)
            constructor_args = abi_encode([a["type"] for a in abi_inputs], values).hex()
            initcode += constructor_args

        return self.deployed_bytecode == execute(initcode, self.source.sender)
