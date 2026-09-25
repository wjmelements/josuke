// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

contract Configured {
    struct Limits {
        address admin;
        uint8 level;
    }

    uint8 public immutable level;
    address public immutable admin;

    constructor(Limits memory limits) {
        level = limits.level;
        admin = limits.admin;
    }

    function configure(Limits calldata limits) external pure returns (uint8) {
        return limits.level;
    }
}
