"""SQLite-backed trace cache — single .db replaces all JSON store/manifest/snapshot files.

Architecture
============
All caching in one SQLite database (.utxo-cache/cache.db), three layers:

1. Global Store (utxos + transactions + address_txns tables)
   Accumulates every UTxONode, transaction, and address→tx mapping ever seen,
   shared across ALL trace types. Cross-cache: data fetched by UTXO trace is
   immediately available to address trace, and vice versa.

2. Trace Manifests (trace_manifests + trace_steps tables)
   Per-trace step-level progress tracking. Interrupted traces resume without
   re-processing completed steps.

3. Trace Snapshots (trace_snapshots table)
   Complete result for display (dash app, CLI summary). Written once at end.

Atomic-level caching: individual UTXOs and TX data are cached, not just full
trace results. Cross-cache is automatic via shared tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, NamedTuple, Optional

from utxo_tracer.models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    Asset,
    OutRef,
    TraceResult,
    TraceStep,
    TransactionEdge,
    UTxONode,
)


class CachedTrace(NamedTuple):
    """Result of a partial trace load."""

    cached_steps: list[tuple[str, int, UTxONode]]
    failed_nodes: list[tuple[str, int, str]]
    cached_max_depth: int
    parent_map: dict[str, Optional[str]]
    trace_key: str
    completed: bool


class CachedAddrTrace(NamedTuple):
    """Partial address trace progress from manifest."""

    processed: set[str]
    failed: set[str]
    total: int
    tx_limit: int
    max_depth: int = 1
    completed: bool = False
    processed_by_addr: dict[str, set[str]] | None = None


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

CACHE_DIR = Path.cwd() / ".utxo-cache"
DB_PATH = CACHE_DIR / "cache.db"

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utxos (
    node_id TEXT PRIMARY KEY,
    tx_hash TEXT NOT NULL,
    output_index INTEGER NOT NULL,
    address TEXT NOT NULL,
    assets TEXT NOT NULL DEFAULT '[]',
    datum_hash TEXT,
    inline_datum TEXT,
    script_ref TEXT,
    ada REAL NOT NULL DEFAULT 0.0,
    lovelace INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_utxos_address ON utxos(address);
CREATE INDEX IF NOT EXISTS idx_utxos_tx_hash ON utxos(tx_hash);

CREATE TABLE IF NOT EXISTS transactions (
    tx_hash TEXT PRIMARY KEY,
    inputs TEXT NOT NULL DEFAULT '[]',
    outputs TEXT NOT NULL DEFAULT '[]',
    input_utxos TEXT,
    block_time INTEGER,
    fetched_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS address_txns (
    address TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    fetched_at REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (address, tx_hash)
);

CREATE TABLE IF NOT EXISTS trace_manifests (
    trace_key TEXT PRIMARY KEY,
    trace_type TEXT NOT NULL CHECK(trace_type IN ('utxo', 'address')),
    start_ref TEXT NOT NULL,
    direction TEXT,
    max_depth INTEGER NOT NULL,
    tx_limit INTEGER DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS trace_steps (
    trace_key TEXT NOT NULL REFERENCES trace_manifests(trace_key) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    depth INTEGER NOT NULL,
    error TEXT,
    parent_key TEXT,
    ts REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (trace_key, step_key)
);

CREATE TABLE IF NOT EXISTS trace_snapshots (
    trace_key TEXT PRIMARY KEY,
    trace_type TEXT NOT NULL CHECK(trace_type IN ('utxo', 'address')),
    metadata TEXT NOT NULL DEFAULT '{}',
    data TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS viz_state (
    cache_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
"""

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# connection management
# ---------------------------------------------------------------------------

_connection: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(str(DB_PATH))
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA foreign_keys=ON")
        _connection.execute("PRAGMA busy_timeout=5000")
        _init_schema(_connection)
    return _connection


def close_db() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# cache keys
# ---------------------------------------------------------------------------


def _cache_key(start_out_ref: OutRef, direction: str, max_depth: int) -> str:
    key_str = (
        f"{start_out_ref.tx_hash}#{start_out_ref.output_index}/{direction}/{max_depth}"
    )
    return hashlib.sha256(key_str.encode()).hexdigest()[:16]


def _addr_cache_key(address: str, max_depth: int = 1) -> str:
    key_str = f"addr:{address}:d{max_depth}"
    return hashlib.sha256(key_str.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# model serialization helpers
# ---------------------------------------------------------------------------


def _node_to_dict(n: UTxONode) -> dict:
    return {
        "id": n.id,
        "out_ref": {
            "tx_hash": n.out_ref.tx_hash,
            "output_index": n.out_ref.output_index,
        },
        "address": n.address,
        "assets": [
            {
                "policy_id": a.policy_id,
                "asset_name": a.asset_name,
                "quantity": a.quantity,
            }
            for a in n.assets
        ],
        "ada": n.ada,
        "lovelace": n.lovelace,
        "datum_hash": n.datum_hash,
        "inline_datum": n.inline_datum,
        "script_ref": n.script_ref,
    }


def _node_from_dict(d: dict) -> UTxONode:
    assets = []
    for a in d.get("assets", []):
        if isinstance(a, dict) and "policy_id" in a:
            assets.append(
                Asset(
                    policy_id=a["policy_id"],
                    asset_name=a.get("asset_name", ""),
                    quantity=a.get("quantity", 0),
                )
            )
        else:
            unit = a.get("unit", "") if isinstance(a, dict) else ""
            qty = a.get("quantity", 0) if isinstance(a, dict) else 0
            if unit == "lovelace" or not unit:
                assets.append(Asset(policy_id="", asset_name="", quantity=qty))
            elif "." in unit:
                policy_id, asset_name = unit.split(".", 1)
                assets.append(
                    Asset(policy_id=policy_id, asset_name=asset_name, quantity=qty)
                )
            else:
                assets.append(Asset(policy_id=unit, asset_name="", quantity=qty))
    return UTxONode(
        id=d["id"],
        out_ref=OutRef(d["out_ref"]["tx_hash"], d["out_ref"]["output_index"]),
        address=d["address"],
        assets=assets,
        datum_hash=d.get("datum_hash"),
        inline_datum=d.get("inline_datum"),
        script_ref=d.get("script_ref"),
    )


# ===================================================================
# CORE CACHE — utxos, transactions, address_txns
# ===================================================================


def save_utxo(node: UTxONode) -> None:
    if not node or not node.address:
        return
    conn = _get_db()
    conn.execute(
        """INSERT OR REPLACE INTO utxos
           (node_id, tx_hash, output_index, address, assets,
            datum_hash, inline_datum, script_ref, ada, lovelace)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            node.id,
            node.out_ref.tx_hash,
            node.out_ref.output_index,
            node.address,
            json.dumps(_node_to_dict(node).get("assets", []), default=str),
            node.datum_hash,
            json.dumps(node.inline_datum, default=str)
            if node.inline_datum is not None
            else None,
            node.script_ref,
            node.ada,
            node.lovelace,
        ),
    )
    conn.commit()


def save_utxos(nodes: list[UTxONode]) -> None:
    if not nodes:
        return
    conn = _get_db()
    rows = []
    for node in nodes:
        if node is None or not node.address:
            continue
        rows.append(
            (
                node.id,
                node.out_ref.tx_hash,
                node.out_ref.output_index,
                node.address,
                json.dumps(_node_to_dict(node).get("assets", []), default=str),
                node.datum_hash,
                json.dumps(node.inline_datum, default=str)
                if node.inline_datum is not None
                else None,
                node.script_ref,
                node.ada,
                node.lovelace,
            )
        )
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO utxos
           (node_id, tx_hash, output_index, address, assets,
            datum_hash, inline_datum, script_ref, ada, lovelace)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def get_utxo(node_id: str) -> Optional[UTxONode]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM utxos WHERE node_id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    return _row_to_utxo(row)


def _row_to_utxo(row: sqlite3.Row) -> UTxONode:
    assets = json.loads(row["assets"]) if row["assets"] else []
    return UTxONode(
        id=row["node_id"],
        out_ref=OutRef(tx_hash=row["tx_hash"], output_index=row["output_index"]),
        address=row["address"],
        assets=[
            Asset(
                policy_id=a.get("policy_id", ""),
                asset_name=a.get("asset_name", ""),
                quantity=a.get("quantity", 0),
            )
            for a in assets
        ],
        datum_hash=row["datum_hash"],
        inline_datum=json.loads(row["inline_datum"]) if row["inline_datum"] else None,
        script_ref=row["script_ref"],
    )


def get_utxos_by_tx_hash(tx_hash: str) -> list[UTxONode]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM utxos WHERE tx_hash = ? ORDER BY output_index", (tx_hash,)
    ).fetchall()
    return [_row_to_utxo(r) for r in rows]


# ── Transaction cache ────────────────────────────────────────────────


def save_transaction(tx_hash: str, tx_data: dict) -> None:
    if not tx_hash:
        return
    inputs = [n.node_id() for n in tx_data.get("inputs", [])]
    outputs = [n.id for n in tx_data.get("outputs", [])]
    input_utxos = tx_data.get("input_utxos", {})
    input_utxos_json = None
    if input_utxos:
        input_utxos_json = json.dumps(
            {nid: _node_to_dict(n) for nid, n in input_utxos.items() if n is not None},
            default=str,
        )
    conn = _get_db()
    conn.execute(
        """INSERT OR REPLACE INTO transactions
           (tx_hash, inputs, outputs, input_utxos, block_time)
           VALUES (?, ?, ?, ?, ?)""",
        (
            tx_hash,
            json.dumps(inputs),
            json.dumps(outputs),
            input_utxos_json,
            tx_data.get("block_time"),
        ),
    )
    conn.commit()


def get_transaction(tx_hash: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM transactions WHERE tx_hash = ?", (tx_hash,)
    ).fetchone()
    if row is None:
        return None

    input_nodes = json.loads(row["inputs"]) if row["inputs"] else []
    inputs = []
    for nid in input_nodes:
        parts = nid.rsplit(":", 1)
        if len(parts) == 2:
            inputs.append(OutRef(tx_hash=parts[0], output_index=int(parts[1])))

    output_nodes = json.loads(row["outputs"]) if row["outputs"] else []
    outputs = []
    for nid in output_nodes:
        utxo = get_utxo(nid)
        if utxo is not None:
            outputs.append(utxo)

    input_utxos = {}
    if row["input_utxos"]:
        raw = json.loads(row["input_utxos"])
        for nid, ndata in raw.items():
            if ndata:
                input_utxos[nid] = _node_from_dict(ndata)

    result = {"inputs": inputs, "outputs": outputs, "input_utxos": input_utxos}
    if row["block_time"] is not None:
        result["block_time"] = row["block_time"]
    return result


# ── Address→tx mapping ────────────────────────────────────────────────


def save_address_txns(address: str, tx_hashes: list[str]) -> None:
    if not address or not tx_hashes:
        return
    conn = _get_db()
    rows = [(address, h) for h in tx_hashes]
    conn.executemany(
        "INSERT OR IGNORE INTO address_txns (address, tx_hash) VALUES (?, ?)",
        rows,
    )
    conn.commit()


def get_address_txns(address: str) -> set[str]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT tx_hash FROM address_txns WHERE address = ?", (address,)
    ).fetchall()
    return {r["tx_hash"] for r in rows}


# ===================================================================
# STORE LOADING — build cache dicts for tracing engines
# ===================================================================


def _store_to_models(
    _unused_store: Optional[dict] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> tuple[dict[str, UTxONode], dict[str, list[str]], dict[str, list[str]]]:
    """Load cached UTXOs + edges into dicts for tracing engines.

    Paginates the UTXO table via ``limit``/``offset`` so callers can
    process large caches incrementally.  By default (``limit=None``)
    loads ALL rows — same behaviour as before.

    Returns (nodes, inputs, outputs) where:
      - nodes: dict[node_id → UTxONode]
      - inputs: dict[node_id → [source_node_id, ...]]  (backward edges)
      - outputs: dict[node_id → [target_node_id, ...]]  (forward edges)
    """
    conn = _get_db()
    query = "SELECT * FROM utxos ORDER BY node_id"
    params: list = []
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    nodes: dict[str, UTxONode] = {}
    for row in rows:
        node = _row_to_utxo(row)
        if node and node.address:
            nodes[node.id] = node

    inputs: dict[str, list[str]] = {}
    outputs: dict[str, list[str]] = {}
    tx_query = "SELECT tx_hash, inputs, outputs FROM transactions"
    tx_params: list = []
    if limit is not None:
        tx_query += " LIMIT ? OFFSET ?"
        tx_params.extend([limit, offset])
    tx_rows = conn.execute(tx_query, tx_params).fetchall()
    for tx_row in tx_rows:
        tx_inputs = json.loads(tx_row["inputs"]) if tx_row["inputs"] else []
        tx_outputs = json.loads(tx_row["outputs"]) if tx_row["outputs"] else []
        for out_nid in tx_outputs:
            for in_nid in tx_inputs:
                inputs.setdefault(out_nid, [])
                if in_nid not in inputs[out_nid]:
                    inputs[out_nid].append(in_nid)
        for in_nid in tx_inputs:
            for out_nid in tx_outputs:
                outputs.setdefault(in_nid, [])
                if out_nid not in outputs[in_nid]:
                    outputs[in_nid].append(out_nid)

    return nodes, inputs, outputs


def load_all_stored(
    limit: Optional[int] = None,
    offset: int = 0,
) -> tuple[dict[str, UTxONode], dict[str, list[str]]]:
    nodes, inputs, _outputs = _store_to_models(limit=limit, offset=offset)
    return nodes, inputs


def store_summary() -> dict:
    conn = _get_db()
    return {
        "nodes": conn.execute("SELECT COUNT(*) FROM utxos").fetchone()[0],
        "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "addresses": conn.execute("SELECT COUNT(*) FROM address_txns").fetchone()[0],
        "manifests": conn.execute("SELECT COUNT(*) FROM trace_manifests").fetchone()[0],
    }


# ===================================================================
# UTXO TRACE MANIFESTS (per-step progress tracking)
# ===================================================================


def save_trace_step(
    trace_key: str,
    node_id: str,
    depth: int,
    error: Optional[str],
    parent_node_id: Optional[str],
    utxo_data: Optional[UTxONode],
    _store: Optional[dict] = None,
    start: str = "",
    direction: str = "",
) -> None:
    conn = _get_db()
    existing = conn.execute(
        "SELECT trace_key FROM trace_manifests WHERE trace_key = ?",
        (trace_key,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO trace_manifests
               (trace_key, trace_type, start_ref, direction, max_depth)
               VALUES (?, 'utxo', ?, ?, ?)""",
            (trace_key, start, direction, depth),
        )

    conn.execute(
        """INSERT OR REPLACE INTO trace_steps
           (trace_key, step_key, depth, error, parent_key, ts)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (trace_key, node_id, depth, error, parent_node_id, time.time()),
    )
    conn.execute(
        """UPDATE trace_manifests SET max_depth = MAX(max_depth, ?), updated_at = ?
           WHERE trace_key = ?""",
        (depth, time.time(), trace_key),
    )
    conn.commit()

    if utxo_data is not None and utxo_data.address:
        save_utxo(utxo_data)


def _find_best_cache(
    start_out_ref: OutRef, direction: str, max_depth: int
) -> tuple[Optional[str], int]:
    """Find best cached manifest — depth-adaptive lookup via SQLite.

    Priority: exact key → any manifest with same start+direction.
    """
    exact_key = _cache_key(start_out_ref, direction, max_depth)
    conn = _get_db()

    row = conn.execute(
        "SELECT max_depth FROM trace_manifests WHERE trace_key = ?",
        (exact_key,),
    ).fetchone()
    if row is not None:
        return exact_key, row["max_depth"]

    start_str = f"{start_out_ref.tx_hash}#{start_out_ref.output_index}"
    rows = conn.execute(
        "SELECT trace_key, max_depth FROM trace_manifests "
        "WHERE trace_type = 'utxo' AND start_ref = ? AND direction = ?",
        (start_str, direction),
    ).fetchall()

    best_key: Optional[str] = None
    best_depth = 0
    for r in rows:
        actual_depth = r["max_depth"]
        if actual_depth >= max_depth:
            return r["trace_key"], actual_depth
        if actual_depth > best_depth:
            best_key = r["trace_key"]
            best_depth = actual_depth

    return best_key, best_depth


def load_trace_partial(
    start_out_ref: OutRef,
    direction: str,
    max_depth: int,
) -> Optional[CachedTrace]:
    """Load partial trace from best-available SQLite cache."""
    trace_key, cached_max_depth = _find_best_cache(start_out_ref, direction, max_depth)
    if not trace_key:
        return None

    conn = _get_db()
    steps_rows = conn.execute(
        "SELECT * FROM trace_steps WHERE trace_key = ?", (trace_key,)
    ).fetchall()
    manifest_row = conn.execute(
        "SELECT * FROM trace_manifests WHERE trace_key = ?", (trace_key,)
    ).fetchone()

    if not steps_rows:
        return None

    cached: list[tuple[str, int, UTxONode]] = []
    failed: list[tuple[str, int, str]] = []
    parent_map: dict[str, Optional[str]] = {}
    actual_max_depth = 0

    for sr in steps_rows:
        step_key = sr["step_key"]
        depth = sr["depth"]
        if depth > max_depth:
            continue
        actual_max_depth = max(actual_max_depth, depth)
        error = sr["error"]
        parent = sr["parent_key"]
        parent_map[step_key] = parent

        if error:
            failed.append((step_key, depth, error))
        else:
            utxo = get_utxo(step_key)
            if utxo is not None:
                cached.append((step_key, depth, utxo))
            else:
                failed.append((step_key, depth, "Node data missing from store"))

    if not cached and not failed:
        return None

    return CachedTrace(
        cached_steps=cached,
        failed_nodes=failed,
        cached_max_depth=actual_max_depth,
        parent_map=parent_map,
        trace_key=trace_key,
        completed=manifest_row["completed"] if manifest_row else False,
    )


def finalize_trace(trace_key: str) -> None:
    """Mark a trace manifest as completed."""
    conn = _get_db()
    conn.execute(
        "UPDATE trace_manifests SET completed = 1, updated_at = ? WHERE trace_key = ?",
        (time.time(), trace_key),
    )
    conn.commit()


# ===================================================================
# UTXO TRACE SNAPSHOTS
# ===================================================================


def save_trace(
    result: TraceResult,
    start_out_ref: OutRef,
    direction: str,
    max_depth: int,
    provider: str = "",
) -> str:
    """Save a v2 trace snapshot to SQLite."""
    key = _cache_key(start_out_ref, direction, max_depth)

    metadata = {
        "key": key,
        "start": f"{start_out_ref.tx_hash}#{start_out_ref.output_index}",
        "direction": direction,
        "max_depth": max_depth,
        "provider": provider,
        "nodes": len(result.nodes),
        "edges": len(result.edges),
        "total_ada": round(sum(n.ada for n in result.nodes), 6),
        "error": result.error,
        "errors_count": result.errors_count,
    }

    snapshot_data = {
        "v": 2,
        "metadata": metadata,
        "node_ids": [n.id for n in result.nodes],
        "edge_meta": [
            {"id": e.id, "direction": e.direction, "tx_hash": e.tx_hash or ""}
            for e in result.edges
        ],
        "traced_path": result.traced_path,
    }

    conn = _get_db()
    conn.execute(
        """INSERT OR REPLACE INTO trace_snapshots
           (trace_key, trace_type, metadata, data)
           VALUES (?, 'utxo', ?, ?)""",
        (
            key,
            json.dumps(metadata, default=str),
            json.dumps(snapshot_data, default=str),
        ),
    )
    conn.commit()

    # Save all nodes to utxos table for cross-cache
    save_utxos(result.nodes)

    return key


def load_trace(
    start_out_ref: OutRef, direction: str, max_depth: int
) -> Optional[TraceResult]:
    """Load a complete trace snapshot from SQLite (fallback to store reconstruction)."""
    key = _cache_key(start_out_ref, direction, max_depth)

    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM trace_snapshots WHERE trace_key = ? AND trace_type = 'utxo'",
        (key,),
    ).fetchone()
    if row is not None:
        data = json.loads(row["data"])
        metadata = json.loads(row["metadata"])
        node_ids = data.get("node_ids", [])
        edge_meta = data.get("edge_meta", [])
        traced_path = data.get("traced_path", [])

        nodes = []
        for nid in node_ids:
            utxo = get_utxo(nid)
            if utxo is not None:
                nodes.append(utxo)

        if nodes:
            edges = []
            for em in edge_meta:
                eid = em["id"]
                if "->" not in eid:
                    continue
                source, target = eid.split("->", 1)
                edges.append(
                    TransactionEdge(
                        id=eid,
                        source=source,
                        target=target,
                        direction=em.get("direction", "input"),
                        tx_hash=em.get("tx_hash", ""),
                    )
                )
            return TraceResult(
                nodes=nodes,
                edges=edges,
                traced_path=traced_path,
                start_out_ref=start_out_ref,
                direction=direction,
                max_depth=max_depth,
                error=metadata.get("error"),
                errors_count=metadata.get("errors_count", 0),
                provider_name=metadata.get("provider", ""),
            )

    # Store reconstruction from SQLite
    return _build_from_store(start_out_ref, direction, max_depth)


def _build_from_store(
    start_out_ref: OutRef, direction: str, max_depth: int
) -> Optional[TraceResult]:
    """Build a TraceResult from the global store (utxos + transactions tables).

    Loads ALL transactions once at the start and re-uses the parsed list
    in both the BFS traversal and edge-construction phases (was reading
    the table on every BFS iteration — ``N+1`` full scans).
    """
    conn = _get_db()
    start_id = start_out_ref.node_id()

    start_node = get_utxo(start_id)
    if start_node is None:
        return None

    # ── load all transactions once ──────────────────────────────────
    tx_rows = conn.execute(
        "SELECT tx_hash, inputs, outputs FROM transactions"
    ).fetchall()
    parsed_txs: list[tuple[str, set[str], set[str]]] = []
    for r in tx_rows:
        h = r["tx_hash"]
        ins = set(json.loads(r["inputs"]) if r["inputs"] else [])
        outs = set(json.loads(r["outputs"]) if r["outputs"] else [])
        parsed_txs.append((h, ins, outs))

    # ── BFS traversal (uses parsed_txs, not fresh SQL per iteration) ─
    visited: set[str] = set()
    q: deque[tuple[str, int]] = deque([(start_id, 0)])
    while q:
        cur_id, depth = q.popleft()
        if cur_id in visited or depth > max_depth:
            continue
        visited.add(cur_id)
        for _tx_hash, tx_inputs, tx_outputs in parsed_txs:
            if direction in ("backward", "both"):
                if cur_id in tx_outputs:
                    for in_nid in tx_inputs:
                        if in_nid not in visited:
                            q.append((in_nid, depth + 1))
            if direction in ("forward", "both"):
                if cur_id in tx_inputs:
                    for out_nid in tx_outputs:
                        if out_nid not in visited:
                            q.append((out_nid, depth + 1))

    if not visited:
        return None

    # ── build nodes ────────────────────────────────────────────────
    nodes = []
    for nid in sorted(visited):
        utxo = get_utxo(nid)
        if utxo is not None:
            nodes.append(utxo)
    if not nodes:
        return None

    # ── build edges (uses same parsed_txs list) ────────────────────
    edges: list[TransactionEdge] = []
    seen_edges: set[str] = set()
    for tx_hash, tx_inputs, tx_outputs in parsed_txs:
        for in_nid in tx_inputs:
            if in_nid in visited:
                for out_nid in tx_outputs:
                    if out_nid in visited:
                        eid = f"{in_nid}->{out_nid}"
                        if eid not in seen_edges:
                            seen_edges.add(eid)
                            edges.append(
                                TransactionEdge(
                                    id=eid,
                                    source=in_nid,
                                    target=out_nid,
                                    direction="input"
                                    if in_nid in visited and out_nid in visited
                                    else "output",
                                    tx_hash=tx_hash,
                                )
                            )

    return TraceResult(
        nodes=nodes,
        edges=edges,
        traced_path=list(visited),
        start_out_ref=start_out_ref,
        direction=direction,
        max_depth=max_depth,
    )


def has_trace(start_out_ref: OutRef, direction: str, max_depth: int) -> bool:
    """Check if a complete trace snapshot exists."""
    key = _cache_key(start_out_ref, direction, max_depth)
    conn = _get_db()
    row = conn.execute(
        "SELECT 1 FROM trace_snapshots WHERE trace_key = ? AND trace_type = 'utxo'",
        (key,),
    ).fetchone()
    return row is not None


def find_node_in_cache(
    node_id: str, direction: str = "backward", max_depth: int = 5
) -> Optional[TraceResult]:
    """Search cache for *node_id* via SQLite."""
    start_node = get_utxo(node_id)
    if start_node is not None:
        parts = node_id.rsplit(":", 1)
        start_out_ref = OutRef(parts[0], int(parts[1]))
        return _build_from_store(start_out_ref, direction, max_depth)
    return None


# ===================================================================
# ADDRESS TRACE CACHE
# ===================================================================


def _find_best_addr_cache(address: str, max_depth: int) -> tuple[Optional[str], int]:
    """Find best cached address trace manifest — depth-adaptive lookup via SQLite.

    Priority: exact key → any manifest with same address.
    """
    exact_key = get_address_trace_manifest_key(address, max_depth)
    conn = _get_db()

    row = conn.execute(
        "SELECT max_depth FROM trace_manifests WHERE trace_key = ?",
        (exact_key,),
    ).fetchone()
    if row is not None:
        return exact_key, row["max_depth"]

    rows = conn.execute(
        "SELECT trace_key, max_depth FROM trace_manifests "
        "WHERE trace_type = 'address' AND start_ref = ?",
        (address,),
    ).fetchall()

    best_key: Optional[str] = None
    best_depth = 0
    for r in rows:
        actual_depth = r["max_depth"]
        if actual_depth >= max_depth:
            return r["trace_key"], actual_depth
        if actual_depth > best_depth:
            best_key = r["trace_key"]
            best_depth = actual_depth

    return best_key, best_depth


def save_address_trace_step(
    address: str,
    tx_hash: str,
    error: Optional[str],
    discovered_utxos: list[UTxONode],
    total_count: int,
    tx_limit: int = 0,
    max_depth: int = 1,
    source_address: str = "",
    depth: int = 0,
) -> None:
    conn = _get_db()
    trace_key = get_address_trace_manifest_key(address, max_depth)

    existing = conn.execute(
        "SELECT * FROM trace_manifests WHERE trace_key = ?", (trace_key,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO trace_manifests
               (trace_key, trace_type, start_ref, direction, max_depth, tx_limit)
               VALUES (?, 'address', ?, '', ?, ?)""",
            (trace_key, address, max_depth, tx_limit),
        )
    else:
        if not error:
            existing_limit = existing["tx_limit"] or 0
            new_limit = tx_limit
            if new_limit and (new_limit > existing_limit or existing_limit == 0):
                conn.execute(
                    "UPDATE trace_manifests SET tx_limit = ?, updated_at = ? WHERE trace_key = ?",
                    (new_limit, time.time(), trace_key),
                )
            if new_limit == 0 and existing_limit != 0:
                conn.execute(
                    "UPDATE trace_manifests SET tx_limit = 0, updated_at = ? WHERE trace_key = ?",
                    (time.time(), trace_key),
                )
            if max_depth > (existing["max_depth"] or 1):
                conn.execute(
                    "UPDATE trace_manifests SET max_depth = ?, updated_at = ? WHERE trace_key = ?",
                    (max_depth, time.time(), trace_key),
                )

    parent = source_address if source_address else address
    conn.execute(
        """INSERT OR REPLACE INTO trace_steps
           (trace_key, step_key, depth, error, parent_key, ts)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (trace_key, tx_hash, depth, error, parent, time.time()),
    )
    conn.commit()

    if discovered_utxos:
        save_utxos(discovered_utxos)


def get_address_trace_manifest_key(address: str, max_depth: int = 1) -> str:
    """Get the manifest trace key for an address trace."""
    return f"addr_{_addr_cache_key(address, max_depth)}"


def load_address_trace_partial(
    address: str, max_depth: int = 1
) -> Optional[CachedAddrTrace]:
    """Load partial address trace progress from SQLite — depth-adaptive.

    Finds the best cached manifest for *address* across any max_depth,
    then loads per-step data including per-address processed tx hashes.
    """
    trace_key, cached_max_depth = _find_best_addr_cache(address, max_depth)
    if not trace_key:
        return None

    conn = _get_db()
    manifest_row = conn.execute(
        "SELECT * FROM trace_manifests WHERE trace_key = ?", (trace_key,)
    ).fetchone()
    if manifest_row is None:
        return None

    steps_rows = conn.execute(
        "SELECT * FROM trace_steps WHERE trace_key = ?", (trace_key,)
    ).fetchall()
    if not steps_rows:
        return None

    processed: set[str] = set()
    failed: set[str] = set()
    processed_by_addr: dict[str, set[str]] = {}

    for sr in steps_rows:
        step_key = sr["step_key"]
        step_depth = sr["depth"]
        if step_depth > max_depth:
            continue
        parent_key = sr["parent_key"]
        if sr["error"]:
            failed.add(step_key)
            # Attribute failed txs to the parent address
            if parent_key:
                if parent_key not in processed_by_addr:
                    processed_by_addr[parent_key] = set()
                processed_by_addr[parent_key].add(step_key)
        else:
            processed.add(step_key)
            if parent_key:
                if parent_key not in processed_by_addr:
                    processed_by_addr[parent_key] = set()
                processed_by_addr[parent_key].add(step_key)

    if not processed and not failed:
        return None

    return CachedAddrTrace(
        processed=processed,
        failed=failed,
        total=len(processed) + len(failed),
        tx_limit=manifest_row["tx_limit"] or 0,
        max_depth=cached_max_depth,
        completed=bool(manifest_row["completed"]),
        processed_by_addr=processed_by_addr if processed_by_addr else None,
    )


def finalize_address_trace(address: str, max_depth: int = 1) -> None:
    """Mark an address trace manifest as completed."""
    trace_key = get_address_trace_manifest_key(address, max_depth)
    conn = _get_db()
    conn.execute(
        "UPDATE trace_manifests SET completed = 1, updated_at = ? WHERE trace_key = ?",
        (time.time(), trace_key),
    )
    conn.commit()


def save_address_trace(
    result: AddressTraceResult, tx_limit: int = 0, max_depth: int = 1
) -> str:
    """Save v2 address trace snapshot to SQLite."""
    key = _addr_cache_key(result.target_address, max_depth)
    suffix = f"_{tx_limit}" if tx_limit else ""
    file_key = f"{key}{suffix}"

    metadata = {
        "target_address": result.target_address[:40],
        "addresses": len(result.addresses),
        "edges": len(result.edges),
        "tx_limit": tx_limit,
        "max_depth": max_depth,
    }

    data = _addr_result_to_dict(result, key)
    data["tx_limit"] = tx_limit
    data["max_depth"] = max_depth

    conn = _get_db()
    conn.execute(
        """INSERT OR REPLACE INTO trace_snapshots
           (trace_key, trace_type, metadata, data)
           VALUES (?, 'address', ?, ?)""",
        (file_key, json.dumps(metadata, default=str), json.dumps(data, default=str)),
    )
    conn.commit()
    return key


def load_address_trace(
    address: str, tx_limit: int = 0, max_depth: int = 1
) -> Optional[AddressTraceResult]:
    """Load a v2 address trace snapshot from SQLite — depth-adaptive.

    Searches for any snapshot matching *address* with stored max_depth >= *max_depth*
    and sufficient tx_limit.  Falls back to exact-key lookup for backward compat.
    """
    key = _addr_cache_key(address, max_depth)
    candidates = [f"{key}_{tx_limit}"] if tx_limit else []
    candidates.append(key)

    conn = _get_db()

    # 1. Try exact key match first
    for file_key in candidates:
        row = conn.execute(
            "SELECT * FROM trace_snapshots WHERE trace_key = ? AND trace_type = 'address'",
            (file_key,),
        ).fetchone()
        if row is not None:
            data = json.loads(row["data"])
            stored_limit = data.get("tx_limit", 0)
            if tx_limit and stored_limit and stored_limit < tx_limit:
                continue
            stored_depth = data.get("max_depth", 1)
            if max_depth > stored_depth:
                continue
            return _addr_result_from_dict(data, address)

    # 2. Search all address snapshots for this address (depth-adaptive)
    rows = conn.execute(
        "SELECT * FROM trace_snapshots WHERE trace_type = 'address'"
    ).fetchall()
    for row in rows:
        data = json.loads(row["data"])
        if data.get("target_address", "") != address:
            continue
        stored_limit = data.get("tx_limit", 0)
        if tx_limit and stored_limit and stored_limit < tx_limit:
            continue
        stored_depth = data.get("max_depth", 1)
        if stored_depth >= max_depth:
            return _addr_result_from_dict(data, address)
    return None


def has_address_trace(address: str) -> bool:
    """Check if an address trace snapshot exists via JSON metadata.

    Queries the ``metadata`` column's ``target_address`` field using
    SQLite JSON1 ``json_extract`` instead of scanning the raw data
    JSON with ``LIKE '%…%'`` (which was an O(n) full-table scan).
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT 1 FROM trace_snapshots "
        "WHERE trace_type = 'address' "
        "AND json_extract(metadata, '$.target_address') = ?",
        (address[:40],),
    ).fetchone()
    return row is not None


def _addr_result_to_dict(result: AddressTraceResult, cache_key: str) -> dict:
    return {
        "v": 2,
        "cache_key": cache_key,
        "target_address": result.target_address,
        "total_transactions": result.total_transactions,
        "error": result.error,
        "provider_name": result.provider_name,
        "max_depth": result.max_depth,
        "addresses": [
            {
                "address": n.address,
                "address_type": n.address_type,
                "total_ada": n.total_ada,
                "net_ada": n.net_ada,
                "total_incoming_ada": n.total_incoming_ada,
                "total_outgoing_ada": n.total_outgoing_ada,
                "tx_count": n.tx_count,
                "is_cex": n.is_cex,
                "cex_name": n.cex_name,
                "is_target": n.is_target,
                "depth": n.depth,
            }
            for n in result.addresses
        ],
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "tx_hashes": e.tx_hashes,
                "interaction_count": e.interaction_count,
                "direction_relative_to_target": e.direction_relative_to_target,
                "source_depth": e.source_depth,
            }
            for e in result.edges
        ],
    }


def _addr_result_from_dict(
    data: dict, target_address: str
) -> Optional[AddressTraceResult]:
    try:
        addresses = [
            AddressInteractionNode(
                address=n["address"],
                address_type=n.get("address_type", "unknown"),
                total_ada=n.get("total_ada", 0.0),
                net_ada=n.get("net_ada", 0.0),
                total_incoming_ada=n.get("total_incoming_ada", 0.0),
                total_outgoing_ada=n.get("total_outgoing_ada", 0.0),
                tx_count=n.get("tx_count", 0),
                is_cex=n.get("is_cex", False),
                cex_name=n.get("cex_name", ""),
                is_target=n.get("is_target", False),
                depth=n.get("depth", 0),
            )
            for n in data.get("addresses", [])
        ]
        edges = [
            AddressInteractionEdge(
                source=e["source"],
                target=e["target"],
                tx_hashes=e.get("tx_hashes", []),
                interaction_count=e.get(
                    "interaction_count", len(e.get("tx_hashes", []))
                ),
                direction_relative_to_target=e.get(
                    "direction_relative_to_target", "unknown"
                ),
                source_depth=e.get("source_depth", 0),
            )
            for e in data.get("edges", [])
        ]
        return AddressTraceResult(
            target_address=target_address,
            addresses=addresses,
            edges=edges,
            total_transactions=data.get(
                "total_transactions", data.get("transactions_examined", 0)
            ),
            error=data.get("error"),
            provider_name=data.get("provider_name", ""),
            max_depth=data.get("max_depth", 1),
        )
    except Exception as e:
        logger.warning("Failed to parse cached address trace: %s", e)
        return None


# ===================================================================
# CACHE LISTING / CLEARING
# ===================================================================


def list_traces() -> list[dict]:
    """List all cached traces from SQLite."""
    entries: list[dict] = []
    conn = _get_db()

    utxo_rows = conn.execute(
        "SELECT * FROM trace_snapshots WHERE trace_type = 'utxo' ORDER BY created_at DESC"
    ).fetchall()
    for row in utxo_rows:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        entries.append(
            {
                "start": metadata.get("start", ""),
                "direction": metadata.get("direction", ""),
                "max_depth": metadata.get("max_depth", ""),
                "nodes": metadata.get("nodes", ""),
                "edges": metadata.get("edges", ""),
                "total_ada": metadata.get("total_ada", ""),
                "provider": metadata.get("provider", ""),
                "exists": True,
                "created_at": row["created_at"],
            }
        )

    addr_rows = conn.execute(
        "SELECT * FROM trace_snapshots WHERE trace_type = 'address' ORDER BY created_at DESC"
    ).fetchall()
    for row in addr_rows:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        entries.append(
            {
                "start": metadata.get("target_address", ""),
                "direction": "address",
                "max_depth": metadata.get("max_depth", ""),
                "nodes": metadata.get("addresses", ""),
                "edges": metadata.get("edges", ""),
                "exists": True,
                "created_at": row["created_at"],
            }
        )

    entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return entries


def clear_cache() -> int:
    """Remove ALL cached data from SQLite."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    conn = _get_db()
    for table in [
        "utxos",
        "transactions",
        "address_txns",
        "trace_steps",
        "trace_manifests",
        "trace_snapshots",
        "viz_state",
    ]:
        cursor = conn.execute(f"DELETE FROM {table}")
        count += cursor.rowcount
    conn.commit()
    try:
        close_db()
        if DB_PATH.exists():
            DB_PATH.unlink()
    except Exception:
        pass
    return count


# ===================================================================
# VIZ STATE PERSISTENCE
# ===================================================================


def save_viz_state(cache_key: str, state: dict) -> None:
    conn = _get_db()
    conn.execute(
        """INSERT OR REPLACE INTO viz_state (cache_key, state, updated_at)
           VALUES (?, ?, ?)""",
        (cache_key, json.dumps(state, default=str), time.time()),
    )
    conn.commit()


def load_viz_state(cache_key: str) -> dict:
    conn = _get_db()
    row = conn.execute(
        "SELECT state FROM viz_state WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if row is not None:
        try:
            return json.loads(row["state"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


# ===================================================================
# CROSS-CACHE bridge
# ===================================================================


def save_utxos_to_store(
    utxos: list[UTxONode], edges: Optional[list[tuple[str, str, str]]] = None
) -> None:
    """Save discovered UTXOs to the global store for cross-cache sharing.

    Called by trace_address_interactions to share UTXO data with
    UTXO traces (and vice versa).
    """
    if utxos:
        save_utxos(utxos)

    if edges:
        conn = _get_db()
        for source, target, direction in edges:
            source_parts = source.rsplit(":", 1)
            target_parts = target.rsplit(":", 1)
            if len(source_parts) == 2 and len(target_parts) == 2:
                tx_hash = source_parts[0]
                existing = conn.execute(
                    "SELECT 1 FROM transactions WHERE tx_hash = ?", (tx_hash,)
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """INSERT OR IGNORE INTO transactions (tx_hash, inputs, outputs)
                           VALUES (?, ?, ?)""",
                        (tx_hash, json.dumps([source]), json.dumps([target])),
                    )
        conn.commit()


__all__ = [
    "_cache_key",
    "_store_to_models",
    "clear_cache",
    "close_db",
    "finalize_address_trace",
    "finalize_trace",
    "find_node_in_cache",
    "get_address_txns",
    "get_transaction",
    "get_utxo",
    "get_utxos_by_tx_hash",
    "has_address_trace",
    "has_trace",
    "list_traces",
    "load_address_trace",
    "load_address_trace_partial",
    "load_all_stored",
    "load_trace",
    "load_trace_partial",
    "load_viz_state",
    "save_address_trace",
    "save_address_trace_step",
    "save_address_txns",
    "save_trace",
    "save_trace_step",
    "save_transaction",
    "save_utxo",
    "save_utxos",
    "save_utxos_to_store",
    "save_viz_state",
    "store_summary",
    "CACHE_DIR",
    "DB_PATH",
]
