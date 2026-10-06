// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// Captures the block and chain it was deployed on, so a replay must reproduce both.
contract DeployedAt {
    uint256 public immutable deployedAt = block.timestamp;
    uint256 public immutable chainId = block.chainid;
}
