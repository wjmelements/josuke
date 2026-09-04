// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// The deployer is captured into an immutable, so the runtime bytecode depends on
// msg.sender at construction. matches_source() must replay the constructor call
// `from` the recorded deployer to recompute identical runtime bytecode.
contract FromDeployer {
    address public immutable deployer;

    constructor() {
        deployer = msg.sender;
    }
}
