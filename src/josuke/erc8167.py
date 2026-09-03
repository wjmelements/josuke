from .selectors import Selector
from .opcodes import ADD, DUP2, DUP3, MSIZE, MSTORE, PUSH0, PUSH1, PUSH4, RETURN, SWAP1, push

# keccak256("selectors()")[:4]
SELECTORS_SELECTOR = "0x6e25b978"


def selectors_selector() -> Selector:
    return Selector(SELECTORS_SELECTOR, "selectors()")


def generated_selectors(facet_selector_lists: list[list[Selector]]) -> list[Selector]:
    """The canonical list the generated `selectors()` returns: every facet
    selector plus `selectors()` itself, de-duped by 4-byte value and sorted
    ascending. `deploy` and `verify` both build the bytecode from this, so the
    order must be stable."""
    by_selector = {SELECTORS_SELECTOR: selectors_selector()}
    for selectors in facet_selector_lists:
        for selector in selectors:
            by_selector.setdefault(selector.selector, selector)
    return [by_selector[key] for key in sorted(by_selector)]


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
