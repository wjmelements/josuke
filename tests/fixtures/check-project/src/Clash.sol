// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Exports `owner()` too, so it clashes with Ownable in one proxy.
contract Clash {
    address public owner;
}

// Same bytecode as Clash under another name.
contract ClashTwin {
    address public owner;
}

interface IClash {
    function owner() external view returns (address);
}
