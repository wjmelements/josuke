import subprocess

from eth_utils import to_checksum_address
from itertools import batched

from .opcodes import DUP1, LOG3, PUSH0, PUSH20, PUSH32, SSTORE
from .selectors import Selector


class ContractSource:
    def __init__(self, path, name):
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
        self.address20 = address[2:].lower()
        self.source = source
        source_map[address] = self

    def __repr__(self) -> str:
        return f"{self.source} (@{self.address})"

    def __eq__(self, other) -> bool:
        return self.address == other.address and self.source == other.source

# event SelectorDelegated(bytes4 indexed selector, address indexed delegate)
PUSH_SELECTOR_DELEGATED = f"{PUSH32}7c86091fe23e473af4b780c37525ce8cfa74ec05a4a4e7d4d3e7f0551d86a7ce"
SET_DELEGATE_SIZE = 125
SELECTOR32_SUFFIX = "0" * 56

class InvalidSetDelegate(Exception):
    """Raised when a bytestring was not produced by SetDelegate.encode."""

# Fixed segments of a SetDelegate encoding.
SET_DELEGATE_PREFIX = bytes.fromhex(PUSH20)  # bytecode[0:1]
SET_DELEGATE_SELECTOR_PUSH = bytes.fromhex(f"{DUP1}{PUSH32}")  # bytecode[21:23]
SET_DELEGATE_SELECTOR_PAD = bytes.fromhex(SELECTOR32_SUFFIX)  # bytecode[27:55]
SET_DELEGATE_LOG = bytes.fromhex(
    f"{PUSH_SELECTOR_DELEGATED}{PUSH0}{PUSH0}{LOG3}{PUSH32}"
)  # bytecode[55:92]
SET_DELEGATE_SUFFIX = bytes.fromhex(SSTORE)  # bytecode[124:125]

class SetDelegate:
    def __init__(self, selector: Selector, storage_key: str, delegate: Delegate):
        self.selector = selector
        self.selector32 = f"{selector[2:]}{SELECTOR32_SUFFIX}"
        self.storage_key32 = storage_key[2:]
        self.delegate = delegate

    def __repr__(self) -> str:
        return f"SetDelegate: {self.selector} to {self.delegate}"

    def __eq__(self, other) -> bool:
        return self.selector == other.selector and self.storage_key32 == other.storage_key32 and self.delegate == other.delegate

    def encode(self) -> bytes:
        return bytes.fromhex(
            f"{PUSH20}{self.delegate.address20}"
            f"{DUP1}{PUSH32}{self.selector32}{PUSH_SELECTOR_DELEGATED}"
            f"{PUSH0}{PUSH0}{LOG3}"
            f"{PUSH32}{self.storage_key32}{SSTORE}"
        )

    @staticmethod
    def decode(bytecode: bytes) -> SetDelegate:
        if (
            len(bytecode) != SET_DELEGATE_SIZE
            or bytecode[0:1] != SET_DELEGATE_PREFIX
            or bytecode[21:23] != SET_DELEGATE_SELECTOR_PUSH
            or bytecode[27:55] != SET_DELEGATE_SELECTOR_PAD
            or bytecode[55:92] != SET_DELEGATE_LOG
            or bytecode[124:125] != SET_DELEGATE_SUFFIX
        ):
            raise InvalidSetDelegate(bytecode)
        address = to_checksum_address(f"0x{bytecode[1:21].hex()}")
        selector = f"0x{bytecode[23:27].hex()}"
        storage_key = f"0x{bytecode[92:124].hex()}"
        delegate = source_map[address]
        return SetDelegate(selector, storage_key, delegate)


class Migration:
    def __init__(self, setdelegates: list[SetDelegate]):
        self.setdelegates = setdelegates

    def encode(self) -> bytes:
        return b''.join(map(setdelegates, lambda setdelegate: setdelegate.encode()))

    def __repr__(self) -> str:
        try:
            return "\n".join([
                str(SetDelegate.decode(b)) for b in batched(self.setdelegates, SET_DELEGATE_SIZE)
            ])
        except:
            return "Failed to decode Migration"
