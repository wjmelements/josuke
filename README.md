# josuke

Automated, verifiable upgrades for [ERC-8167](https://eips.ethereum.org/EIPS/eip-8167) modular dispatch proxies.

## Installation

josuke needs these on `PATH`:

- Python 3.10+
- [Foundry](https://getfoundry.sh)'s `forge`/`cast`
- the [`evm`](https://github.com/wjmelements/evm) assembler/interpreter

```
# EVM assembler/interpreter
git clone --recurse-submodules https://github.com/wjmelements/evm.git
make -C evm bin/evm
sudo install evm/bin/evm /usr/local/bin/evm

# Foundry (forge, cast)
curl -L https://foundry.paradigm.xyz | bash
foundryup

# josuke
git clone https://github.com/wjmelements/josuke.git
pip install ./josuke
```

## CLI

```
josuke init                     # create an empty josuke.json in the cwd
josuke add <address> <facet>...   # add facet sources to a proxy, registering it if new
josuke deploy                    # deploy changed/new facets, record them under `proposed`
josuke verify                    # check the ledger against the chain in $ETH_RPC_URL
josuke accept                    # after migrating, merge `proposed` into `current`
```

Pass `-f/--file` to any command to point at a ledger other than `./josuke.json`.
Pass `--all` to `deploy` to redeploy every facet, even ones whose bytecode is
unchanged.

For each proxy, `deploy` resolves `facetSrc`, builds each facet's init bytecode
from `HEAD` (reusing recorded `constructorArgs`, prompting for any that are
missing), and deploys only those whose bytecode differs from what `current`
records for that chain. When two proxies resolve to the same init bytecode, it
is deployed once and both proxies point at that address.

When no facet in the set implements ERC-8167 `selectors()`, `deploy` generates
that method from the full selector set (every facet selector plus `selectors()`
itself), deploys it with the `evm -C` universal constructor, and records it under
`selectors`.

It then builds the migration script: one `SelectorDelegated` + `SSTORE` per
selector, pointing each at its facet and zeroing any selector dropped since
`current`. Selectors are grouped by facet so the event signature is pushed once
and each facet address once, roughly halving the script's codesize.
Per-selector storage slots come from simulating a dispatch call
against the live proxy with `evm -nx`; selectors come from each facet's ABI. The
script is deployed with the `evm -C` universal constructor unless an identical one
is already recorded under `proposed`.

For a `<path>.evm` facet (evm-assembler source), josuke reads bytecode and ABI
from the artifact its governing Makefile produces, building it with
`make -C <dir> out/<name>.evm/<name>.json` where `<dir>` is the nearest directory
above the source that has a Makefile.

The result is written back as a fresh `proposed` state (facets + `selectors` +
`migration`) stamped with the current git commit; `current` is left untouched.

For a proxy with a pending upgrade, `verify` first prints a summary of what
`proposed` changes: each facet marked new, changed or removed, with its address
and constructor arguments, and its selectors grouped beneath it — selectors not
served by `current` are listed up front. The pass/fail checks follow.

`verify` checks the recorded state against the chain, rebuilding each recorded
`gitCommit` in a throwaway `git worktree`. For `current`: every facet's
`initcodeHash` recomputes from its commit, its `codehash` matches the code at
the recorded address, and the proxy dispatches each of its selectors to that
address. For `proposed`: the same hash checks, plus `proposed.facets` is exactly
what `facetSrc` resolves to, and the on-chain `migration` installs every
proposed selector and zeroes every selector dropped since `current`. A recorded
`selectors` delegate must hold the method josuke generates for that facet set.
It reports all mismatches and exits non-zero if any.

`accept` is run once the migration has executed against the proxy. It checks on
chain that the migration took effect — every `proposed` selector now routes to
its facet (new and unchanged alike) and every selector dropped since `current`
is cleared — then merges `proposed` into `current` so the ledger's `current`
matches the live code, and removes `proposed`. It exits non-zero without
touching the ledger if any check fails.

`deploy` builds with `forge` and broadcasts with `cast`, reading the environment:

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
The deployment script reads it to decide what to redeploy, writes back a `proposed` upgrade, and once the migration is completed, `accept` merges `proposed` into `current`.
Anyone can replay it to verify a live deployment against the source.

### Schema

The full JSON Schema (draft 2020-12) is [`josuke.schema.json`](josuke.schema.json).
The file is an array of proxy entries.

Notes:

- `codehash` answers "is the right code live?" cheaply from-chain; `initcodeHash`
  answers "was it built from this commit with these args?" and is what the
  verifier recomputes from source. A facet with no constructor has
  `initcodeHash == keccak256(initcode)` and omits `constructorArgs`.
- `from` is the deployer address, replayed as `msg.sender` when the verifier
  re-simulates the constructor. It is only needed when an immutable is derived
  from the deployer; omit it otherwise.
- One `gitCommit` covers a whole `deploymentState`. When `proposed` is promoted,
  unchanged facets keep their existing entries, so a long-lived `current` can
  contain facets whose bytecode predates its `gitCommit`; only the facets
  redeployed in that upgrade are guaranteed to match it.
- The proxy `address` is assumed identical across chains; per-chain divergence
  would need a per-deployment address field.
- The migration script carries no hashes because its bytecode is recomputable
  from the `deploymentState`: one delegate-assignment fragment per selector, with
  the `evm -C` universal constructor (`600b380380600b3d393df3`) prepended.
- `selectors` carries no hashes for the same reason: it is the `selectors()`
  method generated from the set of selectors the `deploymentState` installs. It
  is absent when a facet implements `selectors()` itself.

### Example

See [`josuke.example.json`](josuke.example.json).
