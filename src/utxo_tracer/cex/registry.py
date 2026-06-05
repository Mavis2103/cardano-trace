"""CEX address registry."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from ..models import CexInfo

logger = logging.getLogger(__name__)

_registry: dict[str, CexInfo] = {}

# Seed with well-known Cardano exchange addresses (extend as needed).
KNOWN_CEX: dict[str, CexInfo] = {
    # Binance hot wallet — CEX→Cardano: sends FROM this address TO user directly.
    #               Cardano→CEX: user → intermediary → this address (consolidation).
    "addr1vx7j284mqe59w2mka36gf5xq0hvu8ms2989553fk5qh3prcapfpj3": CexInfo(name="Binance", type="exchange", confidence="high"),
}

_CEX_REGISTRY_META_KEY = "cex_address_registry"

# initialize live registry with seeded data
for _addr, _info in KNOWN_CEX.items():
    _registry[_addr] = _info


def register_cex_address(address: str, info: CexInfo) -> None:
    """Register a single CEX address and persist."""
    if not address:
        return
    _registry[address] = info
    _persist_to_cache()


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
    _persist_to_cache()
    return count


def get_all_cex_addresses() -> dict[str, CexInfo]:
    """Return a copy of the registry."""
    return dict(_registry)


# ── Persistence via SQLite cache meta table ─────────────────────────────


def _get_cache_db():
    """Get a connection to the project SQLite cache DB, if it exists.

    Uses the same path logic as ``utxo_tracer.cache._get_db()`` but
    avoids importing the cache module (which would create a circular
    import from the CLI entrypoint).
    """
    db_path = Path.cwd() / ".utxo-cache" / "cache.db"
    if not db_path.exists():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        # Ensure meta table exists (schema init would do this, but
        # we don't want to import cache here)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        return conn
    except Exception:
        return None


def _persist_to_cache() -> None:
    """Save the current CEX registry into the SQLite cache meta table."""
    conn = _get_cache_db()
    if conn is None:
        return
    try:
        payload = json.dumps(
            {addr: {"name": info.name, "type": info.type, "confidence": info.confidence}
             for addr, info in _registry.items()
             if addr not in KNOWN_CEX}  # don't persist known seeds (already built-in)
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (_CEX_REGISTRY_META_KEY, payload),
        )
        conn.commit()
    except Exception:
        logger.debug("Failed to persist CEX registry (non-critical)", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_from_cache() -> int:
    """Load previously-persisted CEX addresses from the cache meta table.

    Returns the number of addresses loaded (0 if none or on error).
    """
    conn = _get_cache_db()
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_CEX_REGISTRY_META_KEY,)
        ).fetchone()
        if row is None:
            return 0
        data = json.loads(row["value"])
        count = 0
        for addr, info in data.items():
            if addr not in _registry:  # don't overwrite KNOWN_CEX values
                _registry[addr] = CexInfo(
                    name=info.get("name", "Unknown"),
                    type=info.get("type", "exchange"),
                    confidence=info.get("confidence", "high"),
                )
                count += 1
        return count
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Auto-load persisted registry on import ──────────────────────────────
#  (must be at module-bottom so all helper functions are defined first)
try:
    _loaded = _load_from_cache()
    if _loaded > 0:
        logger.debug("Loaded %d CEX addresses from persisted cache", _loaded)
except Exception:
    pass
