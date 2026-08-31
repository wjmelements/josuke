// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

contract NoArgs {
    uint256 public value;

    function set(uint256 v) external {
        value = v;
    }
}
