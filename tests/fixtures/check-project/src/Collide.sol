// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Different functions whose signatures share the selector 0x42966c68.
contract Burn {
    function burn(uint256) external {}
}

contract Collate {
    function collate_propagate_storage(bytes16) external {}
}
