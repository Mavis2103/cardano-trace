# UTXO Tracer

**Trace funds across the Cardano blockchain — backward from inputs, forward through spent outputs.**

A CLI tool and interactive graph visualizer for following UTXO chains. Supports multiple blockchain data providers with automatic fallback, CEX address detection, and an interactive Dash Cytoscape graph.

---

## Features

### Tracing
| Feature | Description |
|---------|-------------|
| **Backward tracing** | Walk backward from a UTXO through transaction inputs. Finds where funds **came from** — CEX withdrawal, mining reward, initial distribution. |
| **Forward tracing** | Walk forward through spent outputs. Finds where funds **went** — hacker wallet chain → CEX deposit. Requires kupmios. |
| **Both directions** | Runs backward then forward from the same starting UTXO. Graph shows cash-in and cash-out edges in different colors. |
| **Edge-based deduplication** | Preserves all branches of diamond-shaped transaction patterns (both A→X and B→X edges kept). |
| **Async generators** | Non-blocking streaming trace steps with incremental store updates. |
| **Global store cache** | Accumulates UTXOs/edges across traces — accelerates future traces that revisit the same transactions with zero provider queries. |
| **Input UTXO pre-caching** | When a provider returns full UTXO data alongside input refs, cache it immediately to avoid separate API calls later. |

### Providers
| Provider | Type | Backward | Forward | Auth |
|----------|------|----------|---------|------|
| **Blockfrost** | REST API | ✓ | ✗ | `project_id` / `bearer` / `dmtr-api-key` |
| **Koios** | REST API | ✓ | ✗ | Bearer token (optional) |
| **Maestro** | REST API | ✓ | ✗ | `x-api-key` |
| **UTxORPC** | gRPC SDK | ✓ | ✓ | `x-api-key` / `dmtr-api-key` |
| **Kupmios** | Kupo + Ogmios | ✓ | ✓ | Optional per-service |

### Fallback & Resilience
| Feature | Description |
|---------|-------------|
| **Auto-fallback** | Primary → utxorpc → blockfrost → koios → maestro on failure |
| **Transient retry** | Exponential backoff (0.5s, 1s, 2s) for timeouts, connection errors, gRPC UNAVAILABLE |
| **Capability-aware** | Skips providers that can't do backward tracing (e.g. DumpHistory unavailable) |
| **Non-transient propagation** | ValueError, TypeError, KeyError, rate-limit (429) propagate immediately |

### CLI Commands
| Command | Description |
|---------|-------------|
| `utxo-tracer trace` | Full trace engine with all options |
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

### CEX Detection
| Feature | Description |
|---------|-------------|
| **Built-in registry** | Seeded with well-known exchange addresses (extensible) |
| **Custom registry** | Load addresses from JSON file (dict or list format) |
| **Confidence levels** | High / medium / low per entry |
| **Detection outputs** | Summary panel, node table, depth tree, dedicated CEX findings table, graph visualization |

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

# Kupmios (local node)
export KUPO_URL=http://localhost:1442
export OGMIOS_URL=http://localhost:1337
```

A `.env` file is auto-discovered from the current working directory, parent directories, and `~/.utxo-tracer/.env`.

---

## Usage

### Trace a UTXO

```bash
# Basic backward trace (default direction, depth 5, auto-fallback)
utxo-tracer trace abc123def456...#0

# Specify provider and increase depth
utxo-tracer trace abc123def456...#0 \
    --provider blockfrost \
    --api-key mainnet_XXX \
    --max-depth 10

# Forward trace (requires kupmios)
utxo-tracer trace abc123def456...#0 \
    --provider kupmios \
    --direction forward \
    --max-depth 10

# Trace backward AND forward from the same UTXO
utxo-tracer trace abc123def456...#0 \
    --provider kupmios \
    --direction both
```

### Output formats

```bash
# Default table
utxo-tracer trace abc123...#0

# JSON to stdout
utxo-tracer trace abc123...#0 --output json

# CSV files
utxo-tracer trace abc123...#0 --output csv --export-csv ./my_trace

# Export to JSON file
utxo-tracer trace abc123...#0 --export-json trace.json
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
utxo-tracer trace abc123...#0 --cex-file ./cex_registry.json
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
`primary → utxorpc → blockfrost → koios → maestro`

Transient errors (timeouts, connection failures) are retried with exponential backoff (0.5s, 1s, 2s). Non-transient errors propagate immediately.

### UTxORPC (recommended)

High-throughput gRPC provider supports both backward and forward tracing. Can be self-hosted or used via Demeter.run.

```bash
# Demeter.run
BLOCKFROST_AUTH_TYPE=dmtr-api-key BLOCKFROST_API_KEY=dmtr_XXX \
BLOCKFROST_ENDPOINT_URL=https://cardano-mainnet.blockfrost.io/api/v0 \
utxo-tracer trace abc123...#0 --provider blockfrost
```

### Kupmios (local node)

The only provider that supports native forward tracing. Requires a running [Kupo](https://github.com/cardanosolutions/kupo) and [Ogmios](https://ogmios.dev) instance.

```bash
utxo-tracer trace abc123...#0 \
    --provider kupmios \
    --kupo-url http://localhost:1442 \
    --ogmios-url http://localhost:1337 \
    --direction forward
```

---

## Tracing modes

### Backward tracing (default)

Walks backward from the starting UTXO through transaction inputs. For each UTXO, fetches the transaction that created it, finds all input UTXOs consumed by that transaction, and continues recursively.

Use case: find where funds **came from** — trace back to a CEX withdrawal, mining reward, or initial distribution.

### Forward tracing

Walks forward from the starting UTXO through spent outputs. For each UTXO's address, finds transactions that spent outputs going to that address, and follows their output UTXOs.

Use case: find where funds **went** — trace through a hacker's wallet chain to a CEX deposit.

### Both directions

Runs backward first, then forward from the same starting UTXO. The graph shows both cash-in (backward) and cash-out (forward) edges in different colors.

### Diamond pattern handling

Both tracing engines use **edge-based deduplication** rather than node-based. This preserves all branches of diamond-shaped transaction patterns:

```
     X
    / \
   A   B      Both A→X and B→X edges are kept.
    \ /
     Y
```

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
utxo-tracer trace <tx_hash>#<output_index>
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
│   ├── kupmios.py             # Kupo + Ogmios (local node)
│   └── fallback.py            # Multi-provider fallback with retries
├── tracing/
│   ├── __init__.py            # build_graph_from_steps()
│   ├── backward.py            # Backward trace engine
│   └── forward.py             # Forward trace engine
└── graph/
    ├── __init__.py
    └── dash_app.py            # Dash Cytoscape visualization
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

Tests use `aiken check -m` conventions (see project-level test infrastructure).

---

## License

Internal tool — Tracking UTXO.
