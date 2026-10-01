// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Captures its own address, like UUPSUpgradeable's __self, so the runtime bytecode
// depends on where CREATE put it: the deployer's address and nonce.
contract SelfAddress {
    address public immutable self = address(this);
}
