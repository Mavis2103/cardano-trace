# Golden Test Cases — Address Trace

## golden_depth1.json
`AddressTraceResult` from `trace-address --max-depth 1`.
- 3 addresses: target (depth 0), Binance CEX (depth 1), wallet (depth 1)
- 2 edges: outgoing to CEX, incoming from wallet
- Validates depth-1 trace correctness: target identification, direct counterparty discovery, CEX detection, direction tracking

## golden_depth2.json
`AddressTraceResult` from `trace-address --max-depth 2`.
- 5 addresses: target + 2 direct (depth 1) + 2 indirect (depth 2)
- 4 edges: 2 direct + 2 depth-2 expansions
- Validates depth-2 trace correctness: multi-hop expansion, edge depth attribution, incremental structure over depth 1

## Schema
Both files conform to `AddressTraceResult` (see `src/utxo_tracer/models.py:135-143`):
- `AddressInteractionNode`: address, address_type, ada stats, tx_count, CEX flags, depth
- `AddressInteractionEdge`: source, target, tx_hashes, direction_relative_to_target, source_depth

## Usage
```python
import json

with open("tests/fixtures/golden_depth1.json") as f:
    data = json.load(f)

from utxo_tracer.models import AddressTraceResult, AddressInteractionNode, AddressInteractionEdge

result = AddressTraceResult(
    target_address=data["target_address"],
    addresses=[AddressInteractionNode(**n) for n in data["addresses"]],
    edges=[AddressInteractionEdge(**e) for e in data["edges"]],
    total_transactions=data["total_transactions"],
    error=data.get("error"),
    provider_name=data.get("provider_name", ""),
    max_depth=data["max_depth"],
)
```

## Notes
- Synthetic data — generated from schema, not live chain data (blockfrost returned errors for individual tx lookups on this address)
- Addresses are realistic-format but not real chain addresses (except target, which is real)
- CEX address tagged with `is_cex: true, cex_name: "Binance"` to test CEX detection in trace output
