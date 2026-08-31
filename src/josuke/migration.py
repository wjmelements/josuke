from eth_utils import to_checksum_address
from itertools import batched

from .delegate import Delegate, source_map
from .opcodes import DUP1, LOG3, PUSH0, PUSH1, PUSH20, PUSH4, PUSH32, SHL, SSTORE
from .selectors import Selector



# event SelectorDelegated(bytes4 indexed selector, address indexed delegate)
PUSH_SELECTOR_DELEGATED = f"{PUSH32}7c86091fe23e473af4b780c37525ce8cfa74ec05a4a4e7d4d3e7f0551d86a7ce"
SET_DELEGATE_SIZE = 100

class InvalidSetDelegate(Exception):
    """Raised when a bytestring was not produced by SetDelegate.encode."""

# Fixed segments of a SetDelegate encoding.
SET_DELEGATE_PREFIX = bytes.fromhex(PUSH20)  # bytecode[0:1]
SET_DELEGATE_SELECTOR_PUSH = bytes.fromhex(f"{DUP1}{PUSH4}")  # bytecode[21:23]
SET_DELEGATE_LOG = bytes.fromhex(
    f"{PUSH1}e0{SHL}{PUSH_SELECTOR_DELEGATED}{PUSH0}{PUSH0}{LOG3}{PUSH32}"
)  # bytecode[27:67]
SET_DELEGATE_SUFFIX = bytes.fromhex(SSTORE)  # bytecode[99:100]

class SetDelegate:
    def __init__(self, selector: Selector, storage_key: str, delegate: Delegate):
        self.selector = selector
        self.selector4 = selector.removeprefix("0x")
        self.storage_key32 = storage_key.removeprefix("0x")
        self.delegate = delegate

    def __repr__(self) -> str:
        return f"SetDelegate: {self.selector} to {self.delegate}"

    def __eq__(self, other) -> bool:
        return self.selector == other.selector and self.storage_key32 == other.storage_key32 and self.delegate == other.delegate

    def encode(self) -> bytes:
        return bytes.fromhex(
            f"{PUSH20}{self.delegate.address20}"
            f"{DUP1}{PUSH4}{self.selector4}{PUSH1}e0{SHL}{PUSH_SELECTOR_DELEGATED}"
            f"{PUSH0}{PUSH0}{LOG3}"
            f"{PUSH32}{self.storage_key32}{SSTORE}"
        )

    @staticmethod
    def decode(bytecode: bytes) -> SetDelegate:
        if (
            len(bytecode) != SET_DELEGATE_SIZE
            or bytecode[0:1] != SET_DELEGATE_PREFIX
            or bytecode[21:23] != SET_DELEGATE_SELECTOR_PUSH
            or bytecode[27:67] != SET_DELEGATE_LOG
            or bytecode[99:100] != SET_DELEGATE_SUFFIX
        ):
            raise InvalidSetDelegate(bytecode)
        address = to_checksum_address(f"0x{bytecode[1:21].hex()}")
        selector = f"0x{bytecode[23:27].hex()}"
        storage_key = f"0x{bytecode[67:99].hex()}"
        delegate = source_map[address]
        return SetDelegate(selector, storage_key, delegate)


class Migration:
    def __init__(self, setdelegates: list[SetDelegate]):
        self.setdelegates = setdelegates

    def encode(self) -> bytes:
        return b''.join(setdelegate.encode() for setdelegate in self.setdelegates)

    def __repr__(self) -> str:
        try:
            return "\n".join([
                str(SetDelegate.decode(b)) for b in batched(self.setdelegates, SET_DELEGATE_SIZE)
            ])
        except:
            return "Failed to decode Migration"
