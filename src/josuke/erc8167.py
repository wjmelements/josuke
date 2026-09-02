from .selectors import Selector
from .opcodes import ADD, DUP2, DUP3, MSIZE, MSTORE, PUSH0, PUSH1, PUSH4, RETURN, SWAP1, push

# This method's codesize is minimized for large numbers of selectors (9n + 12)
# It uses a running pointer to write the selectors to memory, at 4 + 32(i+1)
def selectors_method(selectors: list[Selector]) -> bytes:
    return bytes.fromhex(
        f"{PUSH1}20{PUSH1}24"
        f'{f"{DUP2}{ADD}".join([
            f"{PUSH4}{selector.selector.removeprefix('0x')}{DUP2}{MSTORE}"
            for selector in selectors
        ])}'
        f"{push(len(selectors))}{DUP3}{MSTORE}"
        f"{SWAP1}{PUSH0}{MSTORE}"
        f"{MSIZE}{PUSH0}{RETURN}"
    )
