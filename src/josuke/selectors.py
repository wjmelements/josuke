import click
from eth_utils import keccak


def canonical_type(arg: dict) -> str:
    """The ABI type as it appears in a function signature: a struct is its
    component types in parentheses, keeping any array suffix."""
    abi_type = arg["type"]
    if not abi_type.startswith("tuple"):
        return abi_type
    inner = ",".join(canonical_type(component) for component in arg["components"])
    return f"({inner}){abi_type.removeprefix('tuple')}"


# selector -> canonical signature, for every Selector made in this process. Unlike
# internalType names, the canonical signature of one function is the same in every
# revision, so a second signature under a known selector is a 4-byte collision.
_signatures: dict[str, str] = {}


class SelectorCollision(click.ClickException):
    pass


class Selector:
    @staticmethod
    def from_abi(abi: dict):
        assert abi["type"] == "function"
        canonical = f"{abi['name']}({','.join(canonical_type(arg) for arg in abi['inputs'])})"
        expressive = f"{abi['name']}({', '.join(arg.get('internalType', arg['type']) for arg in abi['inputs'])})"
        selector4 = keccak(text=canonical)[:4].hex()
        return Selector("0x" + selector4, expressive, canonical)

    def __init__(self, selector, expressive, canonical=None):
        self.expressive = expressive
        self.selector = selector
        self.canonical = canonical or expressive
        known = _signatures.setdefault(selector, self.canonical)
        if known != self.canonical:
            raise SelectorCollision(f"selector {selector} is both {known} and {self.canonical}")

    def __eq__(self, other):
        return self.selector == other.selector and self.canonical == other.canonical

    def __hash__(self):
        return hash(self.selector)

    def __repr__(self) -> str:
        return f"{self.expressive} [{self.selector}]"

    def encode(self) -> bytes:
        return bytes.fromhex(self.selector)
