// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Immutables are baked into the *runtime* bytecode, so the deployed code
// differs per constructor argument. matches_source() must reproduce the
// exact args to recompute identical runtime bytecode.
contract WithArgs {
    uint256 public immutable a;
    address public immutable b;

    constructor(uint256 _a, address _b) {
        a = _a;
        b = _b;
    }
}
