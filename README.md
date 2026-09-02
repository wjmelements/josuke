# josuke

Automated, verifiable upgrades for [ERC-8167](https://eips.ethereum.org/EIPS/eip-8167) modular dispatch proxies.

## CLI

```
josuke init                     # create an empty josuke.json in the cwd
josuke add <address> <facet>...   # add facet sources to a proxy, registering it if new
josuke deploy                    # deploy changed/new facets, record them under `proposed`
```

Pass `-f/--file` to any command to point at a ledger other than `./josuke.json`.

For each proxy, `deploy` resolves `facetSrc`, builds each facet's init bytecode
from `HEAD` (reusing recorded `constructorArgs`, prompting for any that are
missing), and deploys only those whose bytecode differs from what `current`
records for that chain.

It then builds the migration script: one `SelectorDelegated` + `SSTORE` per
selector, pointing each at its facet and zeroing any selector dropped since
`current`. Per-selector storage slots come from simulating a dispatch call
against the live proxy with `evm -nx`; selectors come from each facet's ABI (for
a raw `.evm` facet, from the matching Foundry artifact). The script is deployed
with the `evm -C` universal constructor unless an identical one is already
recorded under `proposed`.

The result is written back as a fresh `proposed` state (facets + `migration`)
stamped with the current git commit; `current` is left untouched.

It builds with `forge` and broadcasts with `cast`, reading the environment:

| Variable | Purpose |
| --- | --- |
| `ETH_RPC_URL` | Target chain endpoint. Required; also fixes the chain id. |
| `ETH_KEYSTORE_ACCOUNT` | Keystore account name under `~/.foundry/keystores`. |
| `ETH_PASSWORD` | Path to that keystore's password file. |
| `ETH_KEYSTORE` | Path to a keystore file or directory (alternative to the above). |
| `ETH_FROM` | Sender address, e.g. for an unlocked node account. |

The wallet variables are Foundry's own; any wallet `cast` accepts via the
environment works.

## josuke.json

`josuke.json` is the deployment ledger.
It records, per chain, which facet is installed where, what source and constructor arguments produced it, and any pending upgrade.
The deployment script reads it to decide what to redeploy, writes back a `proposed` upgrade, and once the migration is completed, promotes `proposed` to `current`.
Anyone can replay it to verify a live deployment against the source.

### Schema

JSON Schema (draft 2020-12). The file is an array of proxy entries.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://github.com/wjmelements/josuke/blob/main/josuke.schema.json",
  "title": "josuke.json",
  "description": "Deployment ledger for a diamond proxy and its facets, per chain.",
  "type": "array",
  "items": { "$ref": "#/$defs/proxy" },

  "$defs": {
    "address": {
      "type": "string",
      "pattern": "^0x[0-9a-fA-F]{40}$",
      "description": "20-byte EVM address, 0x-prefixed hex."
    },
    "hash32": {
      "type": "string",
      "pattern": "^0x[0-9a-fA-F]{64}$",
      "description": "32-byte keccak-256 digest, 0x-prefixed hex."
    },
    "gitCommit": {
      "type": "string",
      "pattern": "^[0-9a-fA-F]{40}$",
      "description": "Full git commit the facet sources were built from."
    },
    "sourceId": {
      "type": "string",
      "pattern": "^[^*?]+(:[A-Za-z_$][A-Za-z0-9_$]*|\\.evm)$",
      "description": "A concrete facet source with no globs: \"<path>:<ContractName>\" for a Solidity contract, or \"<path>.evm\" for a raw-bytecode facet."
    },

    "proxy": {
      "type": "object",
      "additionalProperties": false,
      "required": ["address", "facetSrc", "deployments"],
      "properties": {
        "address": {
          "$ref": "#/$defs/address",
          "description": "The diamond / proxy contract address."
        },
        "facetSrc": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": { "type": "string" },
          "description": "Source patterns whose contracts are facets of this proxy. Each entry is a glob over .sol files (\"src/facets/*.sol\"), a single \"<path>:<ContractName>\", or a raw-bytecode file (\"<path>.evm\")."
        },
        "deployments": {
          "type": "object",
          "minProperties": 1,
          "propertyNames": { "pattern": "^[1-9][0-9]*$" },
          "additionalProperties": { "$ref": "#/$defs/deploymentHistory" },
          "description": "Keyed by EVM chain id in decimal (\"314\" = Filecoin mainnet)."
        }
      }
    },

    "deploymentHistory": {
      "type": "object",
      "additionalProperties": false,
      "required": ["current"],
      "properties": {
        "current": {
          "$ref": "#/$defs/deploymentState",
          "description": "The facet set currently installed on the proxy."
        },
        "proposed": {
          "$ref": "#/$defs/deploymentState",
          "description": "A pending upgrade awaiting execution. Promoted to \"current\" once migrate(migration) is confirmed."
        }
      }
    },

    "deploymentState": {
      "type": "object",
      "additionalProperties": false,
      "required": ["gitCommit", "facets"],
      "properties": {
        "gitCommit": { "$ref": "#/$defs/gitCommit" },
        "facets": {
          "type": "object",
          "minProperties": 1,
          "propertyNames": { "$ref": "#/$defs/sourceId" },
          "additionalProperties": { "$ref": "#/$defs/facet" }
        },
        "migration": {
          "type": "object",
          "additionalProperties": false,
          "required": ["address"],
          "properties": {
            "address": { "$ref": "#/$defs/address" }
          },
          "description": "The migration script delegatecalled by migrate(). Present on \"proposed\"; retained on \"current\" after promotion. Its bytecode is fully determined by this deploymentState, so only the address is recorded."
        }
      }
    },

    "facet": {
      "type": "object",
      "additionalProperties": false,
      "required": ["codehash", "initcodeHash"],
      "properties": {
        "address": {
          "$ref": "#/$defs/address",
          "description": "Where this facet is deployed. Recorded for facets (re)deployed by the upgrade script."
        },
        "codehash": {
          "$ref": "#/$defs/hash32",
          "description": "keccak-256 of the deployed runtime bytecode, i.e. what EXTCODEHASH returns for `address`."
        },
        "initcodeHash": {
          "$ref": "#/$defs/hash32",
          "description": "keccak-256 of the full creation bytecode: the compiled init bytecode concatenated with the ABI-encoded constructorArgs. Reproducible from gitCommit + constructorArgs, and what the verifier recomputes from source."
        },
        "constructorArgs": {
          "type": "object",
          "minProperties": 1,
          "additionalProperties": true,
          "description": "Constructor arguments keyed by parameter name. Values are JSON encodings of the ABI types: address and bytesN as 0x hex strings, arrays as arrays, integers as JSON number literals. Integer values are fed to eth_abi.encode, which requires a Python int, so they are kept as bare numbers rather than strings; parse this file with an integer-preserving JSON reader (Python's json is fine — JSON.parse and jq lose precision above 2^53)."
        }
      }
    }
  }
}
```

Notes:

- `codehash` answers "is the right code live?" cheaply from-chain; `initcodeHash`
  answers "was it built from this commit with these args?" and is what the
  verifier recomputes from source. A facet with no constructor has
  `initcodeHash == keccak256(initcode)` and omits `constructorArgs`.
- One `gitCommit` covers a whole `deploymentState`. When `proposed` is promoted,
  unchanged facets keep their existing entries, so a long-lived `current` can
  contain facets whose bytecode predates its `gitCommit`; only the facets
  redeployed in that upgrade are guaranteed to match it.
- The proxy `address` is assumed identical across chains; per-chain divergence
  would need a per-deployment address field.
- The migration script carries no hashes because its bytecode is recomputable
  from the `deploymentState`: the runtime is `SetDelegate.encode()` concatenated
  per selector, and the creation bytecode prepends the `evm -C` universal
  constructor (`600b380380600b3d393df3`).

### Example

```json
[
    {
        "address": "0x2222222222222222222222222222222222222222",
        "facetSrc": [
            "src/facets/*.sol",
            "lib/erc8169/implementation.evm",
            "lib/diamond-std/Admin.sol:Owned"
        ],
        "deployments": {
            "314": {
                "current": {
                    "gitCommit": "e910ce4ca320c7c67e71af36b4a7bd2b2a76666a",
                    "facets": {
                        "lib/diamond-std/Admin.sol:Owned": {
                            "codehash": "0x26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae26ae",
                            "initcodeHash": "0x0de10de10de10de10de10de10de10de10de10de10de10de10de10de10de10de1",
                            "constructorArgs": {
                                "owner": "0x0000000000000000000000000000000000000000",
                                "pendingOwner": "0x4a6f6B9fF1fc974096f9063a45Fd12bD5B928AD1"
                            }
                        },
                        "lib/erc8169/implementation.evm": {
                            "codehash": "0xdfb2e9cd33edb3ebcbde59c4734b88174fb1355b78a146dc8f94d40366691047",
                            "initcodeHash": "0x8169816981698169816981698169816981698169816981698169816981698169"
                        },
                        "src/facets/Bicameral.sol:BicameralGovernance": {
                            "codehash": "0x1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f1c9f",
                            "initcodeHash": "0x6a116a116a116a116a116a116a116a116a116a116a116a116a116a116a116a11",
                            "constructorArgs": {
                                "flags": "0x01000000000000000000000008000000000000000000000000000000040006a7",
                                "senators": [
                                    "0x3333333333333333333333333333333333333333",
                                    "0x4444444444444444444444444444444444444444",
                                    "0x5555555555555555555555555555555555555555"
                                ],
                                "threshold": 2
                            }
                        },
                        "src/facets/Bicameral.sol:BicameralView": {
                            "codehash": "0x37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf37bf",
                            "initcodeHash": "0x1e001e001e001e001e001e001e001e001e001e001e001e001e001e001e001e00",
                            "constructorArgs": {
                                "threshold": 2,
                                "quorum": 50000000000000000000
                            }
                        },
                        "src/facets/Token.sol:Token": {
                            "codehash": "0x8642864286428642864286428642864286428642864286428642864286428642",
                            "initcodeHash": "0x70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc70cc",
                            "constructorArgs": {
                                "supply": 10000000000000000000000000,
                                "name": "NewWorldOrder",
                                "symbol": "NWO"
                            }
                        }
                    }
                }
            }
        }
    }
]
```
