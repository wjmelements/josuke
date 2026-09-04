from eth_utils import to_checksum_address
from itertools import groupby

from .delegate import Delegate, source_map
from .opcodes import DUP1, DUP4, DUP5, LOG3, PUSH0, PUSH1, PUSH20, PUSH4, PUSH32, SHL, SSTORE
from .selectors import Selector


# event SelectorDelegated(bytes4 indexed selector, address indexed delegate)
SELECTOR_DELEGATED_SIG = bytes.fromhex(
    "7c86091fe23e473af4b780c37525ce8cfa74ec05a4a4e7d4d3e7f0551d86a7ce"
)

# A migration script is:
#
#   PUSH32 <SelectorDelegated sig>              -- once; stays at the stack base
#   for each facet, its selectors contiguous:
#     PUSH20 <delegate>                         -- once per facet; stays on stack
#     for each selector but the last:
#       DUP1 DUP1                               -- two working copies of <delegate>
#       PUSH4 <selector> PUSH1 e0 SHL           -- topic1: selector << 224
#       DUP5                                    -- topic0: the sig, 5 deep here
#       PUSH0 PUSH0 LOG3                        -- emit SelectorDelegated
#       PUSH32 <storage slot> SSTORE           -- install the route, back to [sig, delegate]
#     the last selector: DUP1 ... DUP4 ...      -- consumes <delegate>, back to [sig]
#
# Pushing the 32-byte sig once and each 20-byte delegate once per facet (rather
# than once per selector) is the whole point: it roughly halves the codesize.
# The last selector in a group dups <delegate> one time fewer, so LOG3 + SSTORE
# consume it and the stack is back to [sig] with no POP.

NORMAL_BLOCK_SIZE = 48
FINAL_BLOCK_SIZE = 47

_PUSH32 = bytes.fromhex(PUSH32)
_PUSH20 = bytes.fromhex(PUSH20)
_TAIL = bytes.fromhex(SSTORE)
_NORMAL_HEAD = bytes.fromhex(f"{DUP1}{DUP1}{PUSH4}")                                       # [0:3]
_NORMAL_MID = bytes.fromhex(f"{PUSH1}e0{SHL}{DUP5}{PUSH0}{PUSH0}{LOG3}{PUSH32}")           # [7:15]
_FINAL_HEAD = bytes.fromhex(f"{DUP1}{PUSH4}")                                              # [0:2]
_FINAL_MID = bytes.fromhex(f"{PUSH1}e0{SHL}{DUP4}{PUSH0}{PUSH0}{LOG3}{PUSH32}")            # [6:14]


class InvalidMigration(Exception):
    """Raised when a bytestring was not produced by Migration.encode."""


class SetDelegate:
    def __init__(self, selector: Selector, storage_key: str, delegate: Delegate):
        self.selector = selector
        self.selector4 = selector.removeprefix("0x")
        self.storage_key32 = storage_key.removeprefix("0x")
        self.delegate = delegate

    def __repr__(self) -> str:
        return f"SetDelegate: {self.selector} to {self.delegate}"

    def __eq__(self, other) -> bool:
        return (
            self.selector == other.selector
            and self.storage_key32 == other.storage_key32
            and self.delegate == other.delegate
        )

    def _block(self, last: bool) -> bytes:
        # `last` consumes <delegate> instead of preserving it: one DUP1, and the
        # sig sits one slot shallower (DUP4, not DUP5).
        dup_delegate = DUP1 if last else f"{DUP1}{DUP1}"
        dup_sig = DUP4 if last else DUP5
        return bytes.fromhex(
            f"{dup_delegate}{PUSH4}{self.selector4}{PUSH1}e0{SHL}{dup_sig}"
            f"{PUSH0}{PUSH0}{LOG3}"
            f"{PUSH32}{self.storage_key32}{SSTORE}"
        )


class Migration:
    def __init__(self, setdelegates: list[SetDelegate]):
        # Group each facet's selectors so its address is pushed once. Sorting by
        # address keeps the encoding deterministic; verify keys by selector, so
        # the order carries no other meaning.
        self.setdelegates = sorted(
            setdelegates, key=lambda sd: (sd.delegate.address20, sd.selector4)
        )

    def encode(self) -> bytes:
        out = _PUSH32 + SELECTOR_DELEGATED_SIG
        for _, group in groupby(self.setdelegates, key=lambda sd: sd.delegate.address20):
            group = list(group)
            out += bytes.fromhex(f"{PUSH20}{group[0].delegate.address20}")
            for sd in group[:-1]:
                out += sd._block(last=False)
            out += group[-1]._block(last=True)
        return out

    @staticmethod
    def decode(bytecode: bytes) -> "Migration":
        if bytecode[:1] != _PUSH32 or bytecode[1:33] != SELECTOR_DELEGATED_SIG:
            raise InvalidMigration(bytecode)

        setdelegates: list[SetDelegate] = []
        i, n = 33, len(bytecode)
        while i < n:
            if bytecode[i : i + 1] != _PUSH20:
                raise InvalidMigration(bytecode)
            address = to_checksum_address(f"0x{bytecode[i + 1 : i + 21].hex()}")
            delegate = source_map[address]
            i += 21

            saw_final = False
            while not saw_final:
                if bytecode[i : i + 2] == _NORMAL_HEAD[:2]:
                    block = bytecode[i : i + NORMAL_BLOCK_SIZE]
                    if (
                        len(block) != NORMAL_BLOCK_SIZE
                        or block[0:3] != _NORMAL_HEAD
                        or block[7:15] != _NORMAL_MID
                        or block[47:48] != _TAIL
                    ):
                        raise InvalidMigration(bytecode)
                    selector = f"0x{block[3:7].hex()}"
                    storage_key = f"0x{block[15:47].hex()}"
                    i += NORMAL_BLOCK_SIZE
                elif bytecode[i : i + 2] == _FINAL_HEAD:
                    block = bytecode[i : i + FINAL_BLOCK_SIZE]
                    if (
                        len(block) != FINAL_BLOCK_SIZE
                        or block[6:14] != _FINAL_MID
                        or block[46:47] != _TAIL
                    ):
                        raise InvalidMigration(bytecode)
                    selector = f"0x{block[2:6].hex()}"
                    storage_key = f"0x{block[14:46].hex()}"
                    i += FINAL_BLOCK_SIZE
                    saw_final = True
                else:
                    raise InvalidMigration(bytecode)  # a facet group with no final block
                setdelegates.append(SetDelegate(selector, storage_key, delegate))

        return Migration(setdelegates)

    def __repr__(self) -> str:
        return "\n".join(str(sd) for sd in self.setdelegates)
