// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

contract Ownable {
    address public owner;

    function transferOwnership(address to) external {
        owner = to;
    }
}
