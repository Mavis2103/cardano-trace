# utxo-tracer

Cardano UTXO chain tracer with auto-fallback and native gRPC support.

## Install

```bash
cd packages/py-tracer
pip install -e .
pip install pyvis   # for visualization (optional, for none mode skip)
```

## Quick Start

```bash
# Auto-fallback (default): tries utxorpc → blockfrost → koios → maestro
utxo-tracer trace abc123...#0

# Single provider
utxo-tracer trace abc123...#0 --provider blockfrost --api-key mainnet_XXX

# UTxORPC gRPC (recommended, highest throughput)
utxo-tracer trace abc123...#0 --provider utxorpc
# with API key:
utxo-tracer trace abc123...#0 --provider utxorpc --api-key YOUR_KEY

# Demeter.run endpoint
utxo-tracer trace abc123...#0 --provider utxorpc \
  --base-url https://cardano-mainnet.utxorpc-m1.demeter.run

# Forward trace (needs kupmios)
utxo-tracer trace abc123...#0 --provider kupmios \
  --kupo-url http://localhost:1442 \
  --ogmios-url http://localhost:1337 \
  --direction forward

# Disable fallback (single provider only)
utxo-tracer trace abc123...#0 --provider utxorpc --no-fallback
```

## Providers

| Provider | Protocol | Type | Rate Limit | Best For |
|---|---|---|---|---|
| `utxorpc` | **gRPC** native | Cloud/Self-host | **High** | Primary (fastest) |
| `blockfrost` | REST API | Cloud | 30 req/min (free) | Fallback |
| `koios` | REST API | Cloud | ~10 req/s | Fallback |
| `maestro` | REST API | Cloud | ~5 req/s | Fallback |
| `kupmios` | REST + gRPC | Local | **Unlimited** | Forward tracing |

## UTxORPC gRPC Setup

UTxORPC 100% chạy trên gRPC (không phải REST). Cài đặt:

**Hosted (Demeter.run)** — miễn phí, throughput cao:
```bash
# Default trong provider — tự động dùng Demeter.run endpoint
utxo-tracer trace abc123...#0 --provider utxorpc --api-key demeter_key
```

**Self-hosted (Docker)** — chạy local hoàn toàn:
```bash
docker run -p 3001:3001 aniqventures/utxorpc-cardano
# Hoặc với volume data:
docker run -p 3001:3001 \
  -v utxorpc-data:/app/config \
  aniqventures/utxorpc-cardano \
  --config /app/config/cardano.yaml
```

**Mainnet hosted API**:
```bash
# Không cần API key cho free tier
utxo-tracer trace abc123...#0 --provider utxorpc --base-url https://mainnet.utxorpc.com
```

## Auto-Fallback

**Default: ON** — Khi provider chính fail, tự động thử provider tiếp theo:

```
Primary → utxorpc → blockfrost → koios → maestro
```

Mỗi query retry 3 lần với exponential backoff (0.5s → 1s → 2s) trước khi sang provider kế.

```
✓ utxorpc is reachable      (primary)
↑ Fallback: utxorpc failed, using blockfrost  (warn log)
```

Disable với `--no-fallback` để dùng single provider duy nhất.

## Visualization

| Mode | Deps | Mô tả |
|---|---|---|
| `pyvis` (default) | `pyvis` | Interactive HTML, chạy local, không cần tài khoản |
| `none` | — | Không hiển thị gì |

## Environment Variables

```bash
UTXO_TRACER_PROVIDER=utxorpc              # default provider
UTXORPC_API_KEY=your_key                  # UTxORPC API key (tùy chọn)
UTXORPC_BASE_URL=https://mainnet.utxorpc.com  # UTxORPC endpoint
BLOCKFROST_API_KEY=mainnet_XXX
KOIOS_API_KEY=your_key
MAESTRO_API_KEY=your_key
```

## CLI Reference

```
# Health check — test kết nối provider
utxo-tracer health --provider utxorpc

# Trace với full options
utxo-tracer trace <tx_hash>#<index> \
  --provider utxorpc \
  --direction backward \
  --max-depth 10 \
  --visualize pyvis \
  --output-html trace_result.html \
  --fallback

# Export data
utxo-tracer trace <tx_hash>#0 --export-json result.json --export-csv result

# Cấu hình mặc định
utxo-tracer config set --provider utxorpc --api-key YOUR_KEY
```