# UTXO Tracer

**Trace funds across the Cardano blockchain — follow a single UTXO backward/forward, or trace all addresses interacting with a target wallet.**

A CLI tool and interactive graph visualizer for two kinds of chain tracing:
- **UTXO tracing** (`trace-utxo`) — walk a single UTXO backward through inputs or forward through spends (chain-following)
- **Address tracing** (`trace-address`) — find all addresses that have interacted with a given address (interaction graph)

Supports multiple blockchain data providers with automatic fallback, CEX address detection, and an interactive Dash Cytoscape graph.

---

## Features

### Tracing
| Feature | Description |
|---------|-------------|
| **Backward tracing** | Walk backward from a UTXO through transaction inputs. Finds where funds **came from** — CEX withdrawal, mining reward, initial distribution. |
| **Forward tracing** | Walk forward through spent outputs. Finds where funds **went** — hacker wallet chain → CEX deposit. Supported by blockfrost / koios / kupmios / minibf. |
| **Both directions** | Runs backward then forward from the same starting UTXO. Graph shows cash-in and cash-out edges in different colors. |
| **Edge-based deduplication** | Preserves all branches of diamond-shaped transaction patterns (both A→X and B→X edges kept). |
| **Async generators** | Non-blocking streaming trace steps with incremental store updates. |
| **Global store cache** | Accumulates UTXOs/edges across traces — accelerates future traces that revisit the same transactions with zero provider queries. |
| **Input UTXO pre-caching** | When a provider returns full UTXO data alongside input refs, cache it immediately to avoid separate API calls later. |
| **Address interaction tracing** | Follow all addresses that interact with a target address. Unlike UTXO tracing (single chain), this builds an interaction graph of all counterparties — useful for 'who does this address deal with' investigations. |

### Providers
| Provider | Type | Backward | Forward | Address | Auth |
|----------|------|----------|---------|---------|------|
| **Blockfrost** | REST API | ✓ | ✓ | ✓ | `project_id` / `bearer` / `dmtr-api-key` |
| **Koios** | REST API | ✓ | ✓ | ✓ | Bearer token (optional) |
| **Maestro** | REST API | ✓ | ✗ | ✓ | `api-key` |
| **Kupmios** | Kupo | ✗ | ✓ | ✓ | Optional (`dmtr-api-key` / Bearer) |
| **UTxORPC** | gRPC SDK | ✓ | ✗ | ✗ | `x-api-key` / `dmtr-api-key` |
| **minibf** (Dolos) | REST API | ✓ | ✓ | ✓ | usually none (`project_id` optional) |

Backward and forward tracing are each gated on the provider's real capability
(`supports_backward` / `supports_forward`):
- **Forward**: blockfrost / koios / kupmios / minibf. (maestro, utxorpc: no forward.)
- **Backward**: blockfrost / koios / maestro / utxorpc / minibf. **kupmios cannot
  trace backward** — Kupo indexes outputs/UTXOs but has no query for a
  transaction's inputs (and Ogmios has no such method either), so backward traces
  fall back to a backward-capable provider.

### Fallback & Resilience
| Feature | Description |
|---------|-------------|
| **Auto-fallback** | Primary → utxorpc → minibf → koios → blockfrost → maestro on failure. kupmios is excluded; minibf is included only when its base URL is configured (else it would add a dead local hop). |
| **Transient retry** | Exponential backoff (0.5s, 1s, 2s) for timeouts, connection errors, gRPC UNAVAILABLE |
| **Capability-aware** | For backward traces, skips providers that can't resolve tx inputs (kupmios, or utxorpc when DumpHistory is unavailable) |
| **Non-transient propagation** | ValueError, TypeError, KeyError, rate-limit (429) propagate immediately |

### CLI Commands
| Command | Description |
|---------|-------------|
| `utxo-tracer trace-utxo` | Trace a single UTXO backwards/forwards through the chain. Follows the cash flow from a UTXO input or spend. |
| `utxo-tracer trace-address` | Trace all addresses that have interacted with a given Cardano address. Builds an interaction graph of all counterparties. |
| `utxo-tracer health` | Check provider connectivity (single or fallback chain) |
| `utxo-tracer assets` | Show asset breakdown for a single UTXO (ADA, native assets, datum, script ref) |
| `utxo-tracer config set` | Save provider credentials persistently to `~/.utxo-tracer/config.json` |
| `utxo-tracer config show` | Display current config (API keys redacted) |
| `utxo-tracer config clear` | Remove saved config |
| `utxo-tracer cache list` | Show all cached traces with metadata |
| `utxo-tracer cache info` | Storage statistics (file count, store nodes, size) |
| `utxo-tracer cache clear` | Remove all cached data |
| `utxo-tracer open <key>` | Re-open a cached trace visualization |

### Output Formats
| Format | Description |
|--------|-------------|
| **Table** | Rich terminal output with summary panel, node table, CEX findings, depth tree |
| **JSON** | Full structured output to stdout or file |
| **CSV** | Separate `_nodes.csv` and `_edges.csv` files for analysis in spreadsheets |

### Trace CLI Options
| Option | Values | Default |
|--------|--------|---------|
| `--provider` | blockfrost, koios, maestro, kupmios, utxorpc | auto-detect |
| `--direction` | backward, forward, both | backward |
| `--max-depth` | integer | 5 |
| `--fallback/--no-fallback` | boolean | on |
| `--output` | table, json, csv | table |
| `--export-json` | file path | — |
| `--export-csv` | file path | — |
| `--cex-file` | JSON file path | — |
| `--depth-report` | flag | off |
| `--no-cache` | flag | off (cached) |
| `--use-proxy` | flag | off |
| `--proxy-url` | URL | http://localhost:3001 |

### Trace Address CLI Options

The `trace-address` subcommand traces ALL addresses that share a transaction
with a given Cardano address (vs. `trace-utxo` which follows a single UTXO).
Useful for "who does this address interact with" investigations.

| Option | Values | Default |
|--------|--------|---------|
| `--provider` | blockfrost, koios, maestro, kupmios, utxorpc | auto-detect |
| `--max-depth` | integer | 1 (direct interactions only) |
| `--tx-limit` | integer | 0 (all transactions) |
| `--output` | table, json, csv | table |
| `--cex-file` | JSON file path | — |
| `--cex-filter` | flag | off (show all interactions) |
| `--no-cache` | flag | off (cached) |

#### `--cex-filter` (large-graph reduction)

When a trace produces many addresses, use `--cex-filter` to keep only
the subgraph that lies on a path from the target to any registered
CEX address. Non-CEX branches and unrelated counterparties are dropped
(post-filter, the unfiltered result is still cached so you can re-run
without the flag).

Use case: scanning a 200-address trace for CEX exposure should return
a focused subgraph, not the whole trace.

Example: `TARGET ─── A1, A2, A3 (unrelated) ─── X1 ─── X2 ─── BINANCE`
→ with `--cex-filter`: keeps only `TARGET → X1 → X2 → BINANCE`.

If the trace's max depth doesn't reach any CEX, the filter shows only
the target (and prints a hint to increase `--max-depth`).

### CEX Detection
| Feature | Description |
|---------|-------------|
| **Built-in registry** | Seeded with well-known exchange addresses (extensible) |
| **Custom registry** | Load addresses from JSON file (dict or list format) |
| **Confidence levels** | High / medium / low per entry |
| **Detection outputs** | Summary panel, node table, depth tree, dedicated CEX findings table, graph visualization |
| **CEX filter** | `--cex-filter` on `trace-address` reduces a large graph to only the CEX-touching branches |

### Visualization (Dash Cytoscape)
| Feature | Description |
|---------|-------------|
| **Start position focus** | Viewport auto-zooms to the starting UTXO |
| **Node shapes** | Circle (wallet), Diamond (script), Triangle (byron), Hexagon (stake), Square (unknown) |
| **Fill color** | SHA-256 hash of address → HSL hue |
| **Gold border** | Starting UTXO |
| **Red border** | CEX address detected |
| **Node size** | Scaled by ADA amount (logarithmic) |
| **Force-directed layout** | Custom Fruchterman-Reingold with 120 iterations |
| **Overlap removal** | Node–node repulsion + node–edge repulsion (dynamic gaps) |
| **Edge styling** | Red arrows for input (backward), green arrows for output (forward) |
| **Click node** | Right-side detail panel: address, address type badge, ADA, lovelace, output ref, CEX info, native assets |
| **Drag nodes** | Interactive repositioning with auto-save on exit |
| **Zoom/pan** | Scroll to zoom, click-drag background to pan |
| **Left legend panels** | Type legend (5 address types), Address list (top 20 by ADA with colored dots), Asset list (top 30 native assets) |
| **Viz state persistence** | Node positions, zoom, and pan saved/restored per cache key |

### Cache System
| Feature | Description |
|---------|-------------|
| **Location** | `.utxo-cache/` in working directory |
| **Structure** | `index.json` (metadata), `store.json` (global UTXO + edge store), `traces/` (thin per-trace), `viz/` (visualization state) |
| **Global store** | Accumulates UTXOs/input edges across all traces (v3 schema) |
| **Incremental update** | Store flushed to disk at step 1 + every 5 steps during tracing |
| **Cache-aware tracing** | Reuses stored nodes/edges, only fetches missing data from providers |
| **Subgraph extraction** | `find_node_in_cache()` — O(1) lookup from store, BFS expansion in any direction |
| **Re-trace on errors** | Detects cached results with errors and re-fetches missing UTXOs |
| **Fresh node dedup** | Skips empty-address error placeholders that would pollute the store |

### Configuration System
| Feature | Description |
|---------|-------------|
| **Priority** | CLI flags > shell env vars > .env file > `~/.utxo-tracer/config.json` > defaults |
| **.env discovery** | Auto-searched from cwd → parent directories → `~/.utxo-tracer/.env` |
| **Auth types** | Blockfrost: project_id, bearer, dmtr-api-key |
| **Persistent save** | `utxo-tracer config set` writes to config.json (atomic temp+rename, chmod 600) |
| **Shell override warning** | Warns when shell env vars silently override .env values |
| **Proxy support** | Route API calls through a local proxy |
| **Demeter.run support** | `dmtr-api-key` auth + endpoint URL for all compatible providers |

---

## Installation

```bash
# Create a virtual environment (Python ≥ 3.11)
python3 -m venv .venv
source .venv/bin/activate

# Install the package
pip install -e .
```

### Dependencies

| Package | Use |
|---------|-----|
| `click` | CLI framework |
| `httpx` | Async HTTP (Blockfrost, Koios, Maestro, Kupo) |
| `rich` | Terminal output tables, panels, progress |
| `dash` + `dash-cytoscape` | Interactive graph visualization |
| `utxorpc` | gRPC client (UTxORPC provider) |
| `grpcio` | gRPC runtime |
| `pandas` | CSV export |
| `python-dotenv` | `.env` file loading |

---

## Configuration

Config priority (highest → lowest):

```
CLI flags > shell env vars > .env file > ~/.utxo-tracer/config.json > defaults
```

### Quick setup

```bash
# Store credentials persistently
utxo-tracer config set --provider blockfrost --api-key MAINNET_XXX

# Set a different provider as default
utxo-tracer config set --provider utxorpc --api-key YOUR_KEY --make-default

# See current config
utxo-tracer config show

# Clear saved config
utxo-tracer config clear
```

### Environment variables

```bash
# Provider selection
export UTXO_TRACER_PROVIDER=blockfrost

# Blockfrost (also supports Demeter.run via dmtr-api-key auth)
export BLOCKFROST_API_KEY=mainnet_XXX
export BLOCKFROST_AUTH_TYPE=project_id
export BLOCKFROST_ENDPOINT_URL=https://cardano-mainnet.blockfrost.io/api/v0

# Koios
export KOIOS_API_KEY=your_key
export KOIOS_BASE_URL=https://api.koios.rest/api/v1

# Maestro
export MAESTRO_API_KEY=your_key
export MAESTRO_BASE_URL=https://mainnet.gomaestro-api.org/v1

# UTxORPC (high-throughput gRPC)
export UTXORPC_API_KEY=your_key
export UTXORPC_BASE_URL=mainnet.utxorpc.com

# Kupmios (local Kupo)
export KUPO_URL=http://localhost:1442

# minibf (local Dolos mini-Blockfrost)
export MINIBF_BASE_URL=http://localhost:50053
```

A `.env` file is auto-discovered from the current working directory, parent directories, and `~/.utxo-tracer/.env`.

---

## Usage

### Trace a UTXO

```bash
# Basic backward trace (default direction, depth 5, auto-fallback)
utxo-tracer trace-utxo abc123def456...#0

# Specify provider and increase depth
utxo-tracer trace-utxo abc123def456...#0 \
    --provider blockfrost \
    --api-key mainnet_XXX \
    --max-depth 10

# Forward trace (kupmios / blockfrost / koios / minibf)
utxo-tracer trace-utxo abc123def456...#0 \
    --provider kupmios \
    --kupo-url http://localhost:1442 \
    --direction forward \
    --max-depth 10

# Trace backward AND forward from the same UTXO
# (use a backward-capable provider; kupmios alone can't trace backward)
utxo-tracer trace-utxo abc123def456...#0 \
    --provider minibf \
    --direction both
```

### Output formats

```bash
# Default table
utxo-tracer trace-utxo abc123...#0

# JSON to stdout
utxo-tracer trace-utxo abc123...#0 --output json

# CSV files
utxo-tracer trace-utxo abc123...#0 --output csv --export-csv ./my_trace

# Export to JSON file
utxo-tracer trace-utxo abc123...#0 --export-json trace.json
```

### Options

```
UTXO format:   <tx_hash>#<output_index>
  --provider     blockfrost | koios | maestro | kupmios | utxorpc
  --direction    backward (default) | forward | both
  --max-depth    Recursion depth (default: 5)
  --fallback     Auto-fallback across providers (default: on)
  --no-fallback  Single provider only
  --output       table | json | csv
  --cex-file     JSON file with exchange address registry
  --depth-report Show node count per depth level
  --no-cache     Skip local cache, always query providers
```

### Trace an Address

Unlike `trace-utxo` (which follows a single UTXO chain), `trace-address` examines **all transactions** involving a given address and builds an interaction graph showing every counterparty and the flow of funds between them.

```bash
# Basic address trace (depth 1 = direct interactions only)
utxo-tracer trace-address addr1...

# Trace deeper — see who interactors also interact with
utxo-tracer trace-address addr1... --max-depth 2 --tx-limit 50

# Filter to only CEX-reaching branches
utxo-tracer trace-address addr1... --max-depth 3 --cex-filter

# Limit transactions per address level for large addresses
utxo-tracer trace-address addr1... --tx-limit 100

# Control flow direction
utxo-tracer trace-address addr1... --direction backward    # upstream: who sent to it
utxo-tracer trace-address addr1... --direction forward     # downstream: who received from it
utxo-tracer trace-address addr1... --direction both        # all interactions (default)
```

### Other commands

```bash
# Check provider connectivity
utxo-tracer health --provider blockfrost

# Show UTXO asset breakdown
utxo-tracer assets abc123...#0

# Manage cache
utxo-tracer cache list
utxo-tracer cache info
utxo-tracer cache clear

# Open cached trace visualization
utxo-tracer open <cache-key>
```

### CEX detection

```bash
# Load CEX addresses from a JSON file
utxo-tracer trace-utxo abc123...#0 --cex-file ./cex_registry.json
```

The JSON file format:

```json
{
  "addr1q9...": {"name": "Binance", "type": "exchange", "confidence": "medium"}
}
```

Or as a list:

```json
[{"address": "addr1q9...", "name": "Binance", "type": "exchange", "confidence": "medium"}]
```

---

## Providers

### Fallback chain

By default, fallback is enabled. If the primary provider fails, the tool tries:
`primary → utxorpc → minibf → koios → blockfrost → maestro`
(kupmios is excluded from auto-fallback; minibf joins the chain only when its
base URL is configured. For backward traces, providers that can't resolve tx
inputs — e.g. kupmios — are skipped.)

Transient errors (timeouts, connection failures) are retried with exponential backoff (0.5s, 1s, 2s). Non-transient errors propagate immediately.

### UTxORPC

High-throughput gRPC provider. **Backward + address** — backward tracing fetches
a transaction's inputs and outputs in one `ReadTx` call (works for spent outputs,
unlike a UTXO-set query). Forward tracing depends on `DumpHistory`, which many
endpoints (e.g. Demeter.run) do not expose. Can be self-hosted (Dolos) or used
via Demeter.run. The URL scheme selects the transport: `https://` → TLS,
`http://` → plaintext.

```bash
# Demeter.run (TLS) — gRPC endpoint + key
UTXORPC_ENDPOINT_URL=https://<your-endpoint>.demeter.run \
UTXORPC_API_KEY=dmtr_XXX \
utxo-tracer trace-utxo abc123...#0 --provider utxorpc

# Self-hosted Dolos (plaintext gRPC)
utxo-tracer trace-utxo abc123...#0 --provider utxorpc \
    --endpoint-url http://localhost:50051
```

### Kupmios (local Kupo)

Forward + address tracing against a running [Kupo](https://github.com/cardanosolutions/kupo)
instance. **No Ogmios** — Kupo's `?spent` filter and `spent_at` metadata give a
UTXO-precise forward spend map on their own. **No backward tracing**: Kupo indexes
outputs/UTXOs but cannot list a transaction's inputs, so backward traces must use
another provider.

```bash
utxo-tracer trace-utxo abc123...#0 \
    --provider kupmios \
    --kupo-url http://localhost:1442 \
    --direction forward
```

Multiple Kupo instances can be rotated by comma-separating `--kupo-url`.

### minibf (Dolos)

Dolos's Blockfrost-compatible REST subset. **Backward + forward + address**, all
via standard Blockfrost routes served at the root path (no `/api/v0` prefix),
usually without auth.

```bash
utxo-tracer trace-utxo abc123...#0 \
    --provider minibf \
    --base-url http://localhost:50053 \
    --direction both
```

---

## Tracing modes

The tool supports two fundamentally different kinds of traces:

| Kind | Command | Scope | Use case |
|------|---------|-------|----------|
| **UTXO trace** | `trace-utxo` | Follows a **single UTXO** backward through its inputs or forward through spends. | "Where did this specific transaction output come from / go to?" |
| **Address trace** | `trace-address` | Follows **all addresses** that shared a transaction with a target address — builds an interaction graph. | "Who does this address interact with?" |

---

### UTXO tracing (trace-utxo)

#### Backward tracing (default)

Walks backward from the starting UTXO through transaction inputs. For each UTXO, fetches the transaction that created it, finds all input UTXOs consumed by that transaction, and continues recursively.

Use case: find where funds **came from** — trace back to a CEX withdrawal, mining reward, or initial distribution.

#### Forward tracing

Walks forward from the starting UTXO through spent outputs. For each UTXO's address, finds transactions that spent outputs going to that address, and follows their output UTXOs.

Use case: find where funds **went** — trace through a hacker's wallet chain to a CEX deposit.

#### Both directions

Runs backward first, then forward from the same starting UTXO. The graph shows both cash-in (backward) and cash-out (forward) edges in different colors.

#### Diamond pattern handling

Both UTXO tracing engines use **edge-based deduplication** rather than node-based. This preserves all branches of diamond-shaped transaction patterns:

```
     X
    / \
   A   B      Both A→X and B→X edges are kept.
    \ /
     Y
```

### Address tracing (trace-address)

`trace-address` is an entirely different mode from UTXO tracing. Instead of following a single UTXO chain, it:

1. **Fetches all transactions** involving the target address (up to `--tx-limit` per level)
2. **Extracts all counterparty addresses** from those transactions (senders and receivers)
3. **Recurses** on each new address up to `--max-depth` (default 1 = direct interactors only)
4. **Builds a directed interaction graph** showing who sent funds to whom and how much

Unlike UTXO tracing where every node is a single UTXO, address tracing nodes are **addresses** and edges represent the net ADA flow between them across all shared transactions.

```bash
utxo-tracer trace-address addr1...                     # direct interactors
utxo-tracer trace-address addr1... --max-depth 2        # interactors-of-interactors
utxo-tracer trace-address addr1... --cex-filter         # only CEX-touching branches
utxo-tracer trace-address addr1... --direction forward  # only downstream flow
```

The `--direction` flag controls which side of the address's transactions to include:
- **backward** — only addresses that **sent** funds to the target (upstream)
- **forward** — only addresses that **received** from the target (downstream)
- **both** — all counterparties (default)

---

## Visualization

After every trace, a Dash Cytoscape graph opens at `http://127.0.0.1:8050`.

### Node encoding

| Visual | Meaning |
|--------|---------|
| **Circle** | Wallet (key hash payment) |
| **Diamond** | Script (smart contract) |
| **Triangle** | Byron legacy address |
| **Hexagon** | Stake reward account |
| **Square** | Unknown address type |
| **Gold border** | Starting UTXO |
| **Red border** | CEX address detected |
| **Fill color** | SHA-256 hash of address → HSL |

### Interactions

- **Click a node** → right-side detail panel shows address, ADA amount, assets, address type badge
- **Drag nodes** → positions are auto-saved on exit
- **Scroll** → zoom in/out
- **Pan** → click and drag background

### Legend panels (left side)

- **Type** — address type badges
- **Address** — top 20 addresses by ADA with color dots
- **Assets** — all native assets found in the trace (up to 30)

---

## Cache system

All trace results are cached locally under `.utxo-cache/` in the working directory.

```
.utxo-cache/
├── index.json      # Metadata index for all cached traces
├── store.json      # Global store: all UTXOs + input edges ever seen
├── traces/         # Per-trace metadata files (thin, references store)
└── viz/            # Saved visualization state (node positions, zoom)
```

The global store (`store.json`) accumulates UTXOs across traces, accelerating future traces that revisit the same transactions. The store uses a v3 schema with proper direction semantics.

```bash
utxo-tracer cache list   # Show all cached traces
utxo-tracer cache info   # Storage statistics
utxo-tracer cache clear  # Remove all cached data
utxo-tracer open <key>   # Re-open a cached trace visualization
```

Use `--no-cache` to skip local cache entirely and always query providers.

---

## Architecture

```
utxo-tracer trace-utxo <tx_hash>#<output_index>
      │
      ▼
   ┌─────────────────────────────────────────┐
   │  CLI (click) — utxo_tracer/cli.py       │
   │  Parses args, resolves provider, runs   │
   │  trace, prints summary, launches Dash.  │
   └────┬────────────────────────────────┬───┘
        │                                │
        ▼                                ▼
   ┌──────────┐              ┌──────────────────┐
   │ Provider │◄────────────►│ Tracing Engine   │
   │ (5 back- │              │ backward /       │
   │ ends)    │              │ forward / both   │
   └──┬───────┘              │ Async generators │
      │                      │ Edge-based dedup │
      ▼                      └──────────────────┘
   ┌──────────┐
   │ Fallback │──► utxorpc → blockfrost → koios → maestro
   │ Provider │    (auto-retry on transient errors)
   └──────────┘
        │
        ▼
   ┌─────────────────────────────────────────┐
   │  Dash Cytoscape Visualization           │
   │  - Force-directed FR layout             │
   │  - Color-coded by address              │
   │  - Shapes by address type               │
   │  - CEX nodes highlighted red            │
   │  - Click node → detail panel            │
   │  - Auto-saves positions on exit         │
   └─────────────────────────────────────────┘
```

---

## Project structure

```
src/utxo_tracer/
├── __init__.py                # Package version
├── cli.py                     # Click CLI entrypoint (main)
├── config.py                  # Config loading (env, file, overrides)
├── cache.py                   # Trace caching & global store
├── models.py                  # Dataclasses (OutRef, Asset, UTxONode, ...)
├── utils.py                   # Address classification, hex/UTF-8 conversion
├── cex/
│   ├── __init__.py
│   └── registry.py            # CEX address registry & matching
├── providers/
│   ├── __init__.py            # build_provider() factory
│   ├── base.py                # Abstract Provider base class
│   ├── blockfrost.py          # Blockfrost REST API
│   ├── koios.py               # Koios REST API
│   ├── maestro.py             # Maestro REST API
│   ├── utxorpc.py             # UTxORPC gRPC (python-sdk)
│   ├── kupmios.py             # Kupo (local node, forward + address)
│   └── fallback.py            # Multi-provider fallback with retries
├── tracing/
│   ├── __init__.py            # build_graph_from_steps()
│   ├── backward.py            # Backward trace engine
│   ├── forward.py             # Forward trace engine
│   └── address_interactions.py # Address-interaction trace engine
└── graph/
    ├── __init__.py
    └── g6_viz.py              # Interactive graph visualization server
```

---

## Development

```bash
# Install in editable mode with dev dependencies
pip install -e .

# The package uses hatchling build backend
# pyproject.toml at project root
```

### Code conventions

- Python 3.11+ with `from __future__ import annotations`
- Async/await throughout for concurrent provider queries
- Edge-based graph deduplication to preserve diamond patterns
- Typed dataclasses for all domain models
- Rich console output with structured tables and progress bars
- Atomic file writes with temp + rename pattern

### Testing

```bash
python -m pytest -q
```

---

## License

Internal tool — Tracking UTXO.
