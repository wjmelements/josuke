from eth_abi import encode as abi_encode
from json import loads

from .ethjsonrpc import eth_get_code
from .evm import execute
from .proc import run

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
        self.deployed_bytecode = eth_get_code(self.address).removeprefix("0x")

    def matches_source(self) -> bool:
        assert self.deployed_bytecode is not None

        source = str(self.source)
        root = self.source.root

        initcode = run(["forge", "inspect", source, "bytecode"], root).strip().removeprefix("0x")

        abi = loads(run(["forge", "inspect", source, "abi", "--json"], root))
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
