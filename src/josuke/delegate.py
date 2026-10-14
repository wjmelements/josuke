class ContractSource:
    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name

    def __repr__(self):
        return f"{self.path}:{self.name}"

    def __eq__(self, other) -> bool:
        return self.path == other.path and self.name == other.name


source_map = {}

class Delegate:
    def __init__(self, address: str, source: ContractSource):
        self.address = address
        self.address20 = address.removeprefix("0x").lower()
        self.source = source
        source_map[address] = self

    def __repr__(self) -> str:
        return f"{self.source} (@{self.address})"

    def __eq__(self, other) -> bool:
        return self.address == other.address and self.source == other.source

