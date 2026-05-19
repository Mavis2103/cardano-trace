"""CEX address registry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..models import CexInfo

_registry: dict[str, CexInfo] = {}

# Seed with well-known Cardano exchange addresses (extend as needed).
KNOWN_CEX: dict[str, CexInfo] = {
    # Binance hot wallet — CEX→Cardano: sends FROM this address TO user directly.
    #               Cardano→CEX: user → intermediary → this address (consolidation).
    "addr1vx7j284mqe59w2mka36gf5xq0hvu8ms2989553fk5qh3prcapfpj3": CexInfo(name="Binance", type="exchange", confidence="high"),
}

# initialize live registry with seeded data
for _addr, _info in KNOWN_CEX.items():
    _registry[_addr] = _info


def register_cex_address(address: str, info: CexInfo) -> None:
    """Register a single CEX address."""
    if not address:
        return
    _registry[address] = info


def identify_cex(address: str) -> Optional[CexInfo]:
    """Return CexInfo if address is registered, else None."""
    if not address:
        return None
    return _registry.get(address)


def load_cex_from_file(path: str) -> int:
    """Load CEX entries from a JSON file. Returns count loaded.

    JSON shape: {"<address>": {"name": "...", "type": "...", "confidence": "..."}}
    Or list: [{"address": "...", "name": "...", "type": "...", "confidence": "..."}]
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CEX registry file not found: {path}")
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    count = 0
    if isinstance(data, dict):
        for addr, info in data.items():
            if not isinstance(info, dict):
                continue
            register_cex_address(
                addr,
                CexInfo(
                    name=info.get("name", "Unknown"),
                    type=info.get("type", "exchange"),
                    confidence=info.get("confidence", "high"),
                ),
            )
            count += 1
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            addr = entry.get("address")
            if not addr:
                continue
            register_cex_address(
                addr,
                CexInfo(
                    name=entry.get("name", "Unknown"),
                    type=entry.get("type", "exchange"),
                    confidence=entry.get("confidence", "high"),
                ),
            )
            count += 1
    return count


def get_all_cex_addresses() -> dict[str, CexInfo]:
    """Return a copy of the registry."""
    return dict(_registry)
