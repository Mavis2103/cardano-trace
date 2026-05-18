"""Trace cache — persist trace results & visualization state locally."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from utxo_tracer.models import Asset, OutRef, TraceResult, TransactionEdge, UTxONode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

CACHE_DIR = Path.cwd() / ".utxo-cache"
TRACES_DIR = CACHE_DIR / "traces"
INDEX_FILE = CACHE_DIR / "index.json"
VIZ_DIR = CACHE_DIR / "viz"
STORE_FILE = CACHE_DIR / "store.json"


def _ensure_dirs() -> None:
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    VIZ_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(start_out_ref: OutRef, direction: str, max_depth: int) -> str:
    key_str = f"{start_out_ref.tx_hash}#{start_out_ref.output_index}/{direction}/{max_depth}"
    return hashlib.sha256(key_str.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# model serialization
# ---------------------------------------------------------------------------

def _node_to_dict(n: UTxONode) -> dict:
    return {
        "id": n.id,
        "out_ref": {"tx_hash": n.out_ref.tx_hash, "output_index": n.out_ref.output_index},
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
    }


def _node_from_dict(d: dict) -> UTxONode:
    assets = []
    for a in d.get("assets", []):
        if isinstance(a, dict) and "policy_id" in a:
            # new format: {policy_id, asset_name, quantity}
            assets.append(Asset(
                policy_id=a["policy_id"],
                asset_name=a.get("asset_name", ""),
                quantity=a.get("quantity", 0),
            ))
        else:
            # legacy format: {unit, quantity} — parse unit back
            unit = a.get("unit", "") if isinstance(a, dict) else ""
            qty = a.get("quantity", 0) if isinstance(a, dict) else 0
            if unit == "lovelace" or not unit:
                assets.append(Asset(policy_id="", asset_name="", quantity=qty))
            elif "." in unit:
                policy_id, asset_name = unit.split(".", 1)
                assets.append(Asset(policy_id=policy_id, asset_name=asset_name, quantity=qty))
            else:
                assets.append(Asset(policy_id=unit, asset_name="", quantity=qty))
    return UTxONode(
        id=d["id"],
        out_ref=OutRef(d["out_ref"]["tx_hash"], d["out_ref"]["output_index"]),
        address=d["address"],
        assets=assets,
    )


def _edge_to_dict(e: TransactionEdge) -> dict:
    return {"source": e.source, "target": e.target, "direction": e.direction, "tx_hash": e.tx_hash or ""}


def _edge_from_dict(d: dict) -> TransactionEdge:
    return TransactionEdge(
        id=d.get("id", f"{d['source']}>{d['target']}"),
        source=d["source"],
        target=d["target"],
        direction=d["direction"],
        tx_hash=d.get("tx_hash", ""),
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write file atomically via temp + rename (Linux-safe)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)


def save_trace(
    result: TraceResult,
    start_out_ref: OutRef,
    direction: str,
    max_depth: int,
    provider: str = "",
) -> str:
    """Save trace result to cache.  Returns cache key."""
    _ensure_dirs()
    key = _cache_key(start_out_ref, direction, max_depth)

    # index entry
    index = _load_index()
    index[key] = {
        "start": f"{start_out_ref.tx_hash}#{start_out_ref.output_index}",
        "direction": direction,
        "max_depth": max_depth,
        "provider": provider,
        "nodes": len(result.nodes),
        "edges": len(result.edges),
        "total_ada": round(sum(n.ada for n in result.nodes), 6),
        "node_ids": [n.id for n in result.nodes],
        "errors_count": result.errors_count,
        "created_at": time.time(),
    }
    _save_index(index)

    # trace data — thin (v2: refs to store, full nodes/edges are in store)
    data = {
        "v": 2,
        "metadata": {
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
        },
        "node_ids": [n.id for n in result.nodes],
        "edge_meta": [
            {"id": e.id, "direction": e.direction, "tx_hash": e.tx_hash or ""}
            for e in result.edges
        ],
        "traced_path": result.traced_path,
    }
    _atomic_write(TRACES_DIR / f"{key}.json", json.dumps(data, indent=2, default=str))

    # Update global store with all nodes + input-edges
    # Skip nodes with empty address (error placeholders that would pollute the store)
    store = _load_store()
    for n in result.nodes:
        if n.address:
            store.setdefault("nodes", {})[n.id] = _node_to_dict(n)
    for e in result.edges:
        if e.direction == "input":
            existing = store.setdefault("inputs", {}).setdefault(e.target, [])
            if e.source not in existing:
                existing.append(e.source)
    _save_store(store)

    return key


def load_trace(start_out_ref: OutRef, direction: str, max_depth: int) -> Optional[TraceResult]:
    """Load trace result from cache, or None if not cached."""
    key = _cache_key(start_out_ref, direction, max_depth)
    path = TRACES_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    meta = data.get("metadata", {})

    # v2 thin format: reconstruct nodes/edges from store
    if data.get("v") == 2:
        store = _load_store()
        store_nodes = store.get("nodes", {})
        node_ids = data.get("node_ids", [])
        nodes = [n for nid in node_ids
                 for n in ([_node_from_dict(store_nodes[nid])]
                           if nid in store_nodes else [])]
        edge_meta = data.get("edge_meta", [])
        edges = []
        for em in edge_meta:
            eid = em["id"]
            if "->" not in eid:
                logger.warning("Malformed edge ID in cached trace %s: %s", key, eid)
                continue
            source, target = eid.split("->", 1)
            edges.append(TransactionEdge(
                id=eid, source=source, target=target,
                direction=em.get("direction", "input"),
                tx_hash=em.get("tx_hash", ""),
            ))
    else:
        # Legacy format: full nodes/edges embedded in file
        nodes = [_node_from_dict(d) for d in data.get("nodes", [])]
        edges = [_edge_from_dict(d) for d in data.get("edges", [])]

    return TraceResult(
        nodes=nodes,
        edges=edges,
        traced_path=data.get("traced_path", []),
        start_out_ref=start_out_ref,
        direction=direction,
        max_depth=max_depth,
        error=meta.get("error"),
        errors_count=meta.get("errors_count", 0),
        provider_name=meta.get("provider", ""),
    )


def has_trace(start_out_ref: OutRef, direction: str, max_depth: int) -> bool:
    return (TRACES_DIR / f"{_cache_key(start_out_ref, direction, max_depth)}.json").exists()


def find_node_in_cache(node_id: str, direction: str = "backward",
                       max_depth: int = 5) -> Optional[TraceResult]:
    """Search global store and cached traces for *node_id* and return a subgraph.

    O(1) lookup via store if available, otherwise falls back to scanning
    the cache index.
    """
    # Fast path: search global store first
    store = _load_store()
    node_dict = store.get("nodes", {}).get(node_id)
    if node_dict:
        # Build adjacency based on direction
        # Store: inputs[parent] = [child_input, ...]
        #   backward: follow inputs[cur] → deeper
        #   forward:  find keys whose values contain cur → shallower
        import collections

        # Reverse index for forward direction: child → [parents (consumers)]
        outputs_of: dict[str, list[str]] = {}
        if direction == "forward":
            for parent, children in store.get("inputs", {}).items():
                for child in children:
                    outputs_of.setdefault(child, []).append(parent)

        visited: set[str] = set()
        q: collections.deque[str] = collections.deque([node_id])
        while q:
            cur = q.popleft()
            if cur in visited:
                continue
            visited.add(cur)
            if direction == "forward":
                for nxt in outputs_of.get(cur, []):
                    if nxt not in visited:
                        q.append(nxt)
            else:
                for src in store.get("inputs", {}).get(cur, []):
                    if src not in visited:
                        q.append(src)
        # Build edges from visited set + store inputs
        sub_edges: list[TransactionEdge] = []
        for nid in visited:
            for src in store.get("inputs", {}).get(nid, []):
                if src in visited:
                    eid = f"{nid}->{src}"
                    sub_edges.append(TransactionEdge(
                        id=eid, source=src, target=nid,
                        direction="input",
                    ))
        start_out_ref = OutRef(node_id.rsplit(":", 1)[0],
                                int(node_id.rsplit(":", 1)[1]))
        return TraceResult(
            nodes=[_node_from_dict(store["nodes"][nid]) for nid in visited
                   if nid in store.get("nodes", {})],
            edges=sub_edges,
            traced_path=list(visited),
            start_out_ref=start_out_ref,
            direction=direction,
            max_depth=max_depth,
        )

    # Legacy: scan trace files
    index = _load_index()
    node_to_key: dict[str, str] = {}
    for ck, meta in index.items():
        for nid in meta.get("node_ids", []):
            node_to_key[nid] = ck

    cache_key = node_to_key.get(node_id)
    if not cache_key:
        return None

    path = TRACES_DIR / f"{cache_key}.json"
    if not path.exists():
        return None

    try:
        full = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Build node+edge maps
    all_nodes = {d["id"]: d for d in full["nodes"]}
    all_edges = full["edges"]

    # BFS/DFS from node_id following edges in the requested direction
    visited: set[str] = set()
    import collections
    q: collections.deque[str] = collections.deque([node_id])
    while q:
        cur = q.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        for e in all_edges:
            if direction in ("backward", "both") and e["target"] == cur:
                if e["source"] not in visited:
                    q.append(e["source"])
            if direction in ("forward", "both") and e["source"] == cur:
                if e["target"] not in visited:
                    q.append(e["target"])

    # Build result
    sub_nodes = [_node_from_dict(all_nodes[nid]) for nid in visited if nid in all_nodes]
    sub_edges = [
        e for e in all_edges
        if e["source"] in visited and e["target"] in visited
    ]
    if not sub_nodes:
        return None

    edge_objs = [_edge_from_dict(ed) for ed in sub_edges]

    return TraceResult(
        nodes=sub_nodes,
        edges=edge_objs,
        traced_path=list(visited),
        start_out_ref=OutRef(node_id.rsplit(":", 1)[0],
                              int(node_id.rsplit(":", 1)[1])),
        direction=direction,
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# cache index
# ---------------------------------------------------------------------------

def _load_index() -> dict:
    try:
        return json.loads(INDEX_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_index(index: dict) -> None:
    _ensure_dirs()
    _atomic_write(INDEX_FILE, json.dumps(index, indent=2, default=str))


# ---------------------------------------------------------------------------
# global store — accumulates all nodes + inputs across all traces
# ---------------------------------------------------------------------------

STORE_VERSION = 3


def _load_store() -> dict:
    """Load global store (all nodes + inputs ever seen)."""
    try:
        return json.loads(STORE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": STORE_VERSION, "nodes": {}, "inputs": {}}


def _save_store(store: dict) -> None:
    _ensure_dirs()
    _atomic_write(STORE_FILE, json.dumps(store, indent=2, default=str, sort_keys=True))


def load_all_stored() -> tuple[dict[str, UTxONode], dict[str, list[str]]]:
    """Load all nodes and input-edges from global store, with migration check.

    Returns (nodes_map, inputs_map) where:
      nodes_map[node_id] = UTxONode
      inputs_map[target_node_id] = [source_node_id, ...]
    """
    store = _load_store()
    _migrate_store(store)
    return _store_to_models(store)


def _migrate_store(store: dict) -> None:
    """Migrate stores that have reversed input direction (v1/v2 → v3)."""
    if store.get("version", 0) < 3 and store.get("inputs"):
        store["inputs"] = {}
        store["version"] = STORE_VERSION
        _save_store(store)


def _store_to_models(
    store: dict,
) -> tuple[dict[str, UTxONode], dict[str, list[str]]]:
    """Convert raw store dict to (nodes_map, inputs_map).  Pure — no I/O."""
    nodes: dict[str, UTxONode] = {}
    for nid, ndata in store.get("nodes", {}).items():
        # Skip nodes with empty address — they're stale error placeholders
        # that would be treated as valid UTXOs and pollute the trace
        if not ndata.get("address"):
            continue
        node = _node_from_dict(ndata)
        if node:
            nodes[nid] = node
    return nodes, store.get("inputs", {})


def store_summary() -> dict:
    """Return summary of global store contents."""
    store = _load_store()
    return {
        "nodes": len(store.get("nodes", {})),
        "inputs": sum(len(v) for v in store.get("inputs", {}).values()),
        "transactions": len(store.get("inputs", {})),
    }


def load_store_file() -> dict:
    """Load the raw store dict (public), with migration if needed."""
    store = _load_store()
    _migrate_store(store)
    return store


def save_store_file(store: dict) -> None:
    """Persist store dict to disk (public)."""
    _save_store(store)


def add_node_to_store(node: UTxONode, store: dict) -> None:
    """Add a single UTxONode to an in-memory store dict."""
    store.setdefault("nodes", {})[node.id] = _node_to_dict(node)


def add_input_to_store(target: str, source: str, store: dict) -> None:
    """Add a single input edge to an in-memory store dict."""
    existing = store.setdefault("inputs", {}).setdefault(target, [])
    if source not in existing:
        existing.append(source)


# ---------------------------------------------------------------------------


def list_traces() -> list[dict]:
    """Return list of cached trace metadata dicts, newest first."""
    index = _load_index()
    # verify files still exist using actual cache keys
    for ck, meta in index.items():
        meta["exists"] = (TRACES_DIR / f"{ck}.json").exists()
    entries = list(index.values())
    entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return entries


def clear_cache() -> int:
    """Remove all cached traces.  Returns count removed."""
    _ensure_dirs()
    count = len(list(TRACES_DIR.glob("*.json")))
    shutil.rmtree(TRACES_DIR)
    shutil.rmtree(VIZ_DIR)
    if INDEX_FILE.exists():
        INDEX_FILE.unlink()
    if STORE_FILE.exists():
        STORE_FILE.unlink()
    TRACES_DIR.mkdir(parents=True)
    return count


# ---------------------------------------------------------------------------
# visualization state persistence
# ---------------------------------------------------------------------------

def save_viz_state(cache_key: str, state: dict) -> None:
    """Save interactive visualization state for a cached trace."""
    _ensure_dirs()
    path = VIZ_DIR / f"{cache_key}.json"
    _atomic_write(path, json.dumps(state, indent=2, default=str))


def load_viz_state(cache_key: str) -> dict:
    """Load saved visualization state, or return empty dict."""
    path = VIZ_DIR / f"{cache_key}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
