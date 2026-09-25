from eth_utils import keccak


def canonical_type(arg: dict) -> str:
    """The ABI type as it appears in a function signature: a struct is its
    component types in parentheses, keeping any array suffix."""
    abi_type = arg["type"]
    if not abi_type.startswith("tuple"):
        return abi_type
    inner = ",".join(canonical_type(component) for component in arg["components"])
    return f"({inner}){abi_type.removeprefix('tuple')}"


class Selector:
    @staticmethod
    def from_abi(abi: dict):
        assert abi["type"] == "function"
        canonical = f"{abi['name']}({','.join(canonical_type(arg) for arg in abi['inputs'])})"
        expressive = f"{abi['name']}({', '.join(arg.get('internalType', arg['type']) for arg in abi['inputs'])})"
        selector4 = keccak(text=canonical)[:4].hex()
        return Selector("0x" + selector4, expressive)

    def __init__(self, selector, expressive):
        self.expressive = expressive
        self.selector = selector

    # Identity is the 4-byte value alone: the same function can carry different
    # internalType names across revisions, and two signatures can share a selector.
    def __eq__(self, other):
        return self.selector == other.selector

    def __hash__(self):
        return hash(self.selector)

    def __repr__(self) -> str:
        return f"{self.expressive} [{self.selector}]"

    def encode(self) -> bytes:
        return bytes.fromhex(self.selector)
