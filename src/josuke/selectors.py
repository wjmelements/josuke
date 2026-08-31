from eth_utils import keccak

selector_map = {}

class Selector:
    @staticmethod
    def from_abi(abi: dict):
        assert abi["type"] == "function"
        canonical = f"{abi['name']}({','.join(list(map(lambda arg: arg['type'], abi['inputs'])))})"
        expressive = f"{abi['name']}({', '.join(list(map(lambda arg: arg['internalType'], abi['inputs'])))})"
        selector4 = keccak(text=canonical)[:4].hex()
        return Selector("0x" + selector4, expressive)

    def __init__(self, selector, expressive):
        self.expressive = expressive
        self.selector = selector
        if selector in selector_map:
            assert selector_map[selector] == self
        else:
            selector_map[selector] = self

    def __eq__(self, other):
        return self.selector == other.selector and self.expressive == other.expressive

    def __repr__(self) -> str:
        return 

    def encode(self) -> bytes:
        return bytes.fromhex(self.selector)
