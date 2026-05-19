"""Trace cache — persist trace results & visualization state locally.

Architecture
============
Three layers of caching:

1. Global Store (store.json, v4)
   Accumulates every UTxONode + edge ever seen, across ALL traces.
   Maps: nodes[node_id], inputs[target]=[sources] (backward), outputs[source]=[targets] (forward)

2. Trace Manifest (manifests/{key}.json, v1)
   Per-trace step-level manifest: which steps completed, which failed,
   their depth, and parent relationship. Updated incrementally per-step.

3. Trace Snapshot (traces/{key}.json, v2)
   Complete graph snapshot for display (dash app, viz). Written once at end.

Flow
====
  save_trace_step()  ← called each time a TraceStep is yielded
  load_trace_partial() ← called at start to find what's cached
  finalize_trace()   ← called when trace completes normally
  save_trace()       ← called at end for display snapshot (v2 format)
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

from utxo_tracer.models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    Asset,
    OutRef,
    TraceResult,
    TransactionEdge,
    UTxONode,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

CACHE_DIR = Path.cwd() / ".utxo-cache"
TRACES_DIR = CACHE_DIR / "traces"
MANIFESTS_DIR = CACHE_DIR / "manifests"
INDEX_FILE = CACHE_DIR / "index.json"
VIZ_DIR = CACHE_DIR / "viz"
STORE_FILE = CACHE_DIR / "store.json"


def _ensure_dirs() -> None:
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    VIZ_DIR.mkdir(parents=True, exist_ok=True)
    # Clean up stale .tmp files from interrupted atomic writes
    for d in [CACHE_DIR, TRACES_DIR, VIZ_DIR, MANIFESTS_DIR]:
        for p in Path(d).glob("*.tmp"):
            try:
                p.unlink()
            except OSError:
                pass


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
            assets.append(Asset(
                policy_id=a["policy_id"],
                asset_name=a.get("asset_name", ""),
                quantity=a.get("quantity", 0),
            ))
        else:
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
# I/O helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    """Write file atomically via temp + fsync + rename (Linux-safe)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.rename(path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# global store — accumulates all nodes + edges across all traces
# ---------------------------------------------------------------------------

STORE_VERSION = 4


def _load_store() -> dict:
    try:
        return json.loads(STORE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": STORE_VERSION, "nodes": {}, "inputs": {}, "outputs": {}}


def _save_store(store: dict) -> None:
    _ensure_dirs()
    _atomic_write(STORE_FILE, json.dumps(store, indent=2, default=str, sort_keys=True))


def _migrate_store(store: dict) -> None:
    ver = store.get("version", 0)
    if ver < STORE_VERSION:
        if ver < 3 and store.get("inputs"):
            store["inputs"] = {}
        if ver < 4:
            store["outputs"] = store.get("outputs", {})
        store["version"] = STORE_VERSION
        _save_store(store)


def _store_to_models(
    store: dict,
) -> tuple[dict[str, UTxONode], dict[str, list[str]], dict[str, list[str]]]:
    nodes: dict[str, UTxONode] = {}
    for nid, ndata in store.get("nodes", {}).items():
        if not ndata.get("address"):
            continue
        node = _node_from_dict(ndata)
        if node:
            nodes[nid] = node
    return nodes, store.get("inputs", {}), store.get("outputs", {})


def load_all_stored() -> tuple[dict[str, UTxONode], dict[str, list[str]]]:
    store = _load_store()
    _migrate_store(store)
    nodes, inputs, _outputs = _store_to_models(store)
    return nodes, inputs


def store_summary() -> dict:
    store = _load_store()
    return {
        "nodes": len(store.get("nodes", {})),
        "inputs": sum(len(v) for v in store.get("inputs", {}).values()),
        "outputs": sum(len(v) for v in store.get("outputs", {}).values()),
        "transactions": max(len(store.get("inputs", {})), len(store.get("outputs", {}))),
    }


def load_store_file() -> dict:
    store = _load_store()
    _migrate_store(store)
    return store


def save_store_file(store: dict) -> None:
    _save_store(store)


def add_node_to_store(node: UTxONode, store: dict) -> None:
    store.setdefault("nodes", {})[node.id] = _node_to_dict(node)


def add_edge_to_store(source: str, target: str, direction: str, store: dict) -> None:
    """Add an edge to global store with direction tag.

    Backward (direction='backward'): source=input ancestor, target=produced UTXO
      → inputs[target] += [source]  ("target was produced by consuming source")

    Forward (direction='forward'): source=consumed UTXO, target=produced output
      → outputs[source] += [target]  ("source was consumed to produce target")
    """
    if direction == "backward":
        existing = store.setdefault("inputs", {}).setdefault(target, [])
        if source not in existing:
            existing.append(source)
    elif direction == "forward":
        existing = store.setdefault("outputs", {}).setdefault(source, [])
        if target not in existing:
            existing.append(target)


# ===================================================================
# PER-STEP CACHE (v1 manifest)
# ===================================================================

def _manifest_path(trace_key: str) -> Path:
    return MANIFESTS_DIR / f"{trace_key}.json"


def _load_manifest(trace_key: str) -> Optional[dict]:
    path = _manifest_path(trace_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_trace_step(
    trace_key: str,
    node_id: str,
    depth: int,
    error: Optional[str],
    parent_node_id: Optional[str],
    utxo_data: Optional[UTxONode],
    store: dict,
    start: str = "",
    direction: str = "",
) -> None:
    """Save a single traced step to cache.

    1. Create/update trace manifest (records step outcome)
    2. If utxo_data is valid, save node to global store
    3. Atomic write — the manifest is updated incrementally

    Called once per TraceStep yielded by the tracing generator.
    This is the core of per-step caching: even if the trace is
    interrupted, every step completed so far is saved.
    """
    _ensure_dirs()
    _ensure_manifest_dirs()

    path = _manifest_path(trace_key)
    if path.exists():
        manifest = json.loads(path.read_text())
    else:
        manifest = _new_manifest(trace_key, start=start, direction=direction)

    # Record step result
    steps = manifest.setdefault("steps", {})
    step_order = manifest.setdefault("step_order", [])

    steps[node_id] = {
        "depth": depth,
        "error": error,
        "ts": time.time(),
        "parent": parent_node_id,
    }
    if node_id not in step_order:
        step_order.append(node_id)

    _atomic_write(path, json.dumps(manifest, indent=2, default=str))

    # Save node to global store (persist on first discovery only)
    if utxo_data is not None and utxo_data.address:
        if node_id not in store.get("nodes", {}):
            store.setdefault("nodes", {})[node_id] = _node_to_dict(utxo_data)
            _save_store(store)


def _new_manifest(trace_key: str, start: str = "", direction: str = "") -> dict:
    return {
        "v": 1,
        "trace_key": trace_key,
        "start": start,
        "direction": direction,
        "completed": False,
        "created_at": time.time(),
        "steps": {},
        "step_order": [],
    }


def _ensure_manifest_dirs() -> None:
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)


class CachedTrace(NamedTuple):
    """Result of a partial trace load.

    Attributes:
        cached_steps: list of (node_id, depth, UTxONode) — completed steps
        failed_nodes: list of (node_id, depth, error) — steps with errors
        cached_max_depth: int — deepest depth found in manifest
        parent_map: dict[node_id] = parent_node_id — graph structure
        trace_key: str — key of the found cache
        completed: bool — whether the original trace finished normally
    """
    cached_steps: list[tuple[str, int, UTxONode]]
    failed_nodes: list[tuple[str, int, str]]
    cached_max_depth: int
    parent_map: dict[str, Optional[str]]
    trace_key: str
    completed: bool


def _find_best_cache(start_out_ref: OutRef, direction: str, max_depth: int) -> tuple[Optional[str], int]:
    """Find best cached manifest for this query — depth-adaptive lookup.

    Priority:
    1. Exact key match (same start+direction+depth)
    2. Any manifest with same start+direction and deeper depth (subset)
    3. Any manifest with same start+direction (partial, may need deeper)

    Returns (trace_key, cached_max_depth) or (None, 0).
    """
    exact_key = _cache_key(start_out_ref, direction, max_depth)
    exact_path = _manifest_path(exact_key)
    if exact_path.exists():
        return exact_key, max_depth

    # Scan manifests directory — each manifest has start+direction embedded
    if not MANIFESTS_DIR.exists():
        return None, 0

    start_str = f"{start_out_ref.tx_hash}#{start_out_ref.output_index}"
    best_key = None
    best_depth = 0

    for mf in MANIFESTS_DIR.glob("*.json"):
        try:
            mdata = json.loads(mf.read_text())
            if not mdata.get("steps"):
                continue
            m_start = mdata.get("start", "")
            m_dir = mdata.get("direction", "")
            if m_start == start_str and m_dir == direction:
                # Found matching manifest — compute actual max depth
                actual_depth = max(
                    (s.get("depth", 0) for s in mdata.get("steps", {}).values()),
                    default=0,
                )
                if actual_depth >= max_depth:
                    return mdata.get("trace_key", mf.stem), actual_depth
                if actual_depth > best_depth:
                    best_key = mdata.get("trace_key", mf.stem)
                    best_depth = actual_depth
        except (json.JSONDecodeError, OSError):
            continue

    return best_key, best_depth


def load_trace_partial(
    start_out_ref: OutRef,
    direction: str,
    max_depth: int,
) -> Optional[CachedTrace]:
    """Load partial trace from best-available cache.

    Returns CachedTrace with:
    - Steps at depth <= max_depth (subset if cache is deeper)
    - Failed steps identified for re-query
    - Parent map for graph reconstruction

    Returns None only if no cache exists at all.
    """
    trace_key, cached_max_depth = _find_best_cache(start_out_ref, direction, max_depth)
    if not trace_key:
        return None

    manifest = _load_manifest(trace_key)
    if not manifest or not manifest.get("steps"):
        return None

    store = _load_store()
    store_nodes = store.get("nodes", {})

    steps_data = manifest["steps"]
    step_order = manifest.get("step_order", [])

    cached: list[tuple[str, int, UTxONode]] = []
    failed: list[tuple[str, int, str]] = []
    parent_map: dict[str, Optional[str]] = {}
    actual_max_depth = 0

    for nid in step_order:
        sd = steps_data.get(nid)
        if sd is None:
            continue
        depth = sd.get("depth", 0)
        if depth > max_depth:
            continue  # beyond requested depth, skip
        actual_max_depth = max(actual_max_depth, depth)

        error = sd.get("error")
        parent = sd.get("parent")
        parent_map[nid] = parent

        if error:
            failed.append((nid, depth, error))
        elif nid in store_nodes:
            node = _node_from_dict(store_nodes[nid])
            if node:
                cached.append((nid, depth, node))
            else:
                failed.append((nid, depth, "Node parse failed"))
        else:
            # Success in manifest but node missing from store
            failed.append((nid, depth, "Node data missing from store"))

    if not cached and not failed:
        return None

    return CachedTrace(
        cached_steps=cached,
        failed_nodes=failed,
        cached_max_depth=actual_max_depth,
        parent_map=parent_map,
        trace_key=trace_key,
        completed=manifest.get("completed", False),
    )


def finalize_trace(trace_key: str) -> None:
    """Mark a trace manifest as completed (trace finished without interruption)."""
    manifest = _load_manifest(trace_key)
    if manifest is None:
        return
    manifest["completed"] = True
    _atomic_write(_manifest_path(trace_key), json.dumps(manifest, indent=2, default=str))


# ===================================================================
# ORIGINAL TRACE SNAPSHOT (v2 format — for display/viz)
# ===================================================================

def save_trace(
    result: TraceResult,
    start_out_ref: OutRef,
    direction: str,
    max_depth: int,
    provider: str = "",
) -> str:
    """Save v2 trace snapshot for display (dash, viz). Also updates index."""
    _ensure_dirs()
    key = _cache_key(start_out_ref, direction, max_depth)

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

    # Also update store (duplicate-safe via dict merge)
    store = _load_store()
    for n in result.nodes:
        if n.address:
            store.setdefault("nodes", {})[n.id] = _node_to_dict(n)
    for e in result.edges:
        if e.direction == "input":
            add_edge_to_store(e.source, e.target, "backward", store)
        elif e.direction == "output":
            add_edge_to_store(e.source, e.target, "forward", store)
    _save_store(store)

    return key


def _load_trace_file(
    path: Path,
    start_out_ref: OutRef,
    direction: str,
    max_depth: int,
    key: str,
) -> Optional[TraceResult]:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    meta = data.get("metadata", {})

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
        nodes = [_node_from_dict(d) for d in data.get("nodes", [])]
        edges = [_edge_from_dict(d) for d in data.get("edges", [])]

    if not nodes:
        logger.warning("Cache file %s exists but empty nodes — store may be stale", key)

    return TraceResult(
        nodes=nodes, edges=edges, traced_path=data.get("traced_path", []),
        start_out_ref=start_out_ref, direction=direction, max_depth=max_depth,
        error=meta.get("error"), errors_count=meta.get("errors_count", 0),
        provider_name=meta.get("provider", ""),
    )


def load_trace(start_out_ref: OutRef, direction: str, max_depth: int) -> Optional[TraceResult]:
    """Load a complete v2 trace snapshot from cache."""
    key = _cache_key(start_out_ref, direction, max_depth)
    path = TRACES_DIR / f"{key}.json"
    if path.exists():
        result = _load_trace_file(path, start_out_ref, direction, max_depth, key)
        if result is not None and result.nodes:
            return result

    # Depth-progressive fallback via global store
    store = _load_store()
    store_nodes = store.get("nodes", {})
    store_inputs = store.get("inputs", {})
    store_outputs = store.get("outputs", {})
    if not store_nodes:
        return None

    start_id = start_out_ref.node_id()
    if start_id not in store_nodes:
        return None

    from collections import deque
    visited: set[str] = set()
    q: deque[tuple[str, int]] = deque([(start_id, 0)])
    while q:
        cur_id, depth = q.popleft()
        if cur_id in visited or depth > max_depth:
            continue
        visited.add(cur_id)
        if direction == "forward":
            for nxt in store_outputs.get(cur_id, []):
                if nxt not in visited:
                    q.append((nxt, depth + 1))
        else:
            for src in store_inputs.get(cur_id, []):
                if src not in visited:
                    q.append((src, depth + 1))

    if not visited:
        return None

    nodes = [_node_from_dict(store_nodes[nid])
             for nid in sorted(visited) if nid in store_nodes]
    if not nodes:
        return None

    edges: list[TransactionEdge] = []
    if direction == "forward":
        for nid in visited:
            for nxt in store_outputs.get(nid, []):
                if nxt in visited:
                    edges.append(TransactionEdge(
                        id=f"{nid}->{nxt}", source=nid, target=nxt,
                        direction="output",
                    ))
    else:
        for nid in visited:
            for src in store_inputs.get(nid, []):
                if src in visited:
                    edges.append(TransactionEdge(
                        id=f"{src}->{nid}", source=src, target=nid,
                        direction="input",
                    ))
    return TraceResult(
        nodes=nodes, edges=edges, traced_path=list(visited),
        start_out_ref=start_out_ref, direction=direction, max_depth=max_depth,
    )


def has_trace(start_out_ref: OutRef, direction: str, max_depth: int) -> bool:
    """Check if a complete trace snapshot exists."""
    fp = TRACES_DIR / f"{_cache_key(start_out_ref, direction, max_depth)}.json"
    if fp.exists():
        try:
            data = json.loads(fp.read_text())
            if data.get("v") == 2:
                store = _load_store()
                if data.get("node_ids") and any(
                    nid in store.get("nodes", {}) for nid in data["node_ids"]
                ):
                    return True
            elif data.get("v") == 1:
                return True
            else:
                if data.get("nodes"):
                    return True
        except (json.JSONDecodeError, OSError):
            pass
        return False

    start_str = f"{start_out_ref.tx_hash}#{start_out_ref.output_index}"
    index = _load_index()
    for meta in index.values():
        if (meta.get("start") == start_str
                and meta.get("direction") == direction
                and meta.get("max_depth", 0) >= max_depth):
            return True
    return False


def find_node_in_cache(node_id: str, direction: str = "backward",
                       max_depth: int = 5) -> Optional[TraceResult]:
    """Search global store and cached traces for *node_id* and return a subgraph."""
    store = _load_store()
    node_dict = store.get("nodes", {}).get(node_id)
    if node_dict:
        from collections import deque
        store_inputs = store.get("inputs", {})
        store_outputs = store.get("outputs", {})
        visited: set[str] = {node_id}
        q: deque[str] = deque([node_id])
        while q:
            cur = q.popleft()
            if direction == "forward":
                for nxt in store_outputs.get(cur, []):
                    if nxt not in visited:
                        visited.add(nxt)
                        q.append(nxt)
            else:
                for src in store_inputs.get(cur, []):
                    if src not in visited:
                        visited.add(src)
                        q.append(src)

        sub_edges: list[TransactionEdge] = []
        if direction == "forward":
            for nid in visited:
                for nxt in store_outputs.get(nid, []):
                    if nxt in visited:
                        sub_edges.append(TransactionEdge(
                            id=f"{nid}->{nxt}", source=nid, target=nxt,
                            direction="output",
                        ))
        else:
            for nid in visited:
                for src in store_inputs.get(nid, []):
                    if src in visited:
                        sub_edges.append(TransactionEdge(
                            id=f"{src}->{nid}", source=src, target=nid,
                            direction="input",
                        ))

        parts = node_id.rsplit(":", 1)
        start_out_ref = OutRef(parts[0], int(parts[1]))
        found_nodes = [_node_from_dict(store["nodes"][nid]) for nid in visited
                       if nid in store.get("nodes", {})]
        if not found_nodes:
            return None
        return TraceResult(
            nodes=found_nodes, edges=sub_edges, traced_path=list(visited),
            start_out_ref=start_out_ref, direction=direction, max_depth=max_depth,
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
    ikey_meta = index.get(cache_key, {})
    start_str = ikey_meta.get("start", "")
    if not start_str or "#" not in start_str:
        return None
    parts = start_str.rsplit("#", 1)
    start_out_ref = OutRef(parts[0], int(parts[1]))
    trace = _load_trace_file(path, start_out_ref,
                              ikey_meta.get("direction", "backward"),
                              ikey_meta.get("max_depth", 5), cache_key)
    if trace is None or not trace.nodes:
        return None
    visited_ids: set[str] = {node_id}
    from collections import deque as _deque
    qq: _deque[str] = _deque([node_id])
    while qq:
        cur = qq.popleft()
        for e in trace.edges:
            if direction in ("backward", "both") and e.target == cur:
                if e.source not in visited_ids:
                    visited_ids.add(e.source)
                    qq.append(e.source)
            if direction in ("forward", "both") and e.source == cur:
                if e.target not in visited_ids:
                    visited_ids.add(e.target)
                    qq.append(e.target)
    sub_nodes = [n for n in trace.nodes if n.id in visited_ids]
    sub_edges = [e for e in trace.edges
                 if e.source in visited_ids and e.target in visited_ids]
    if not sub_nodes:
        return None
    return TraceResult(
        nodes=sub_nodes, edges=sub_edges, traced_path=list(visited_ids),
        start_out_ref=start_out_ref, direction=direction, max_depth=max_depth,
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
# UTXO trace listing / clearing
# ---------------------------------------------------------------------------


def list_traces() -> list[dict]:
    index = _load_index()
    for ck, meta in index.items():
        meta["exists"] = (TRACES_DIR / f"{ck}.json").exists()
    entries = list(index.values())
    entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return entries


def clear_cache() -> int:
    _ensure_dirs()
    count = len(list(TRACES_DIR.glob("*.json"))) + len(list(MANIFESTS_DIR.glob("*.json")))
    shutil.rmtree(TRACES_DIR)
    shutil.rmtree(VIZ_DIR)
    shutil.rmtree(MANIFESTS_DIR)
    for f in [INDEX_FILE, STORE_FILE, ADDR_INDEX_FILE]:
        if f.exists():
            f.unlink()
    TRACES_DIR.mkdir(parents=True)
    return count


# ---------------------------------------------------------------------------
# address trace cache — incremental manifest (v1) + snapshot (v2)
# ---------------------------------------------------------------------------

ADDR_INDEX_FILE = CACHE_DIR / "addr_index.json"


def _addr_cache_key(address: str) -> str:
    key_str = f"addr:{address}"
    return hashlib.sha256(key_str.encode()).hexdigest()[:16]


def _addr_manifest_key(address: str) -> str:
    return f"addr_{_addr_cache_key(address)}"


def _addr_manifest_path(address: str) -> Path:
    return MANIFESTS_DIR / f"{_addr_manifest_key(address)}.json"


def save_address_trace_step(
    address: str,
    tx_hash: str,
    error: Optional[str],
    discovered_utxos: list[UTxONode],
    total_count: int,
    tx_limit: int = 0,
) -> None:
    """Save single-step progress for an address trace (like save_trace_step).

    Creates/updates a v1 manifest tracking which tx hashes have been
    processed and which failed. Also saves discovered UTXOs to the
    global store for cross-cache sharing.

    Called once per tx hash processed by :func:`trace_address_interactions`.
    This allows interrupted traces to resume without re-processing
    already-completed transactions.

    Args:
        address: The Cardano address being traced.
        tx_hash: On-chain transaction hash just processed.
        error: Error string if the tx fetch failed, else None.
        discovered_utxos: UTXOs discovered in this transaction (cross-cache).
        total_count: Total number of tx hashes known for this address.
        tx_limit: The ``--tx-limit`` value at time of run (0 = no limit).
            Stored in the manifest so later runs with a larger limit
            know to extend instead of loading stale cache.
    """
    _ensure_dirs()
    path = _addr_manifest_path(address)
    try:
        if path.exists():
            manifest = json.loads(path.read_text())
        else:
            manifest = {
                "v": 1,
                "address": address,
                "total_tx_count": total_count,
                "tx_limit": tx_limit,
                "completed": False,
                "created_at": time.time(),
                "tx_hashes_processed": [],
                "tx_hashes_failed": [],
            }

        if error:
            if tx_hash not in manifest["tx_hashes_failed"]:
                manifest["tx_hashes_failed"].append(tx_hash)
        else:
            if tx_hash not in manifest["tx_hashes_processed"]:
                manifest["tx_hashes_processed"].append(tx_hash)
            # Only update tx_limit on successful steps — prevents a
            # rate-limited extension from recording a larger limit
            # that would block future extension attempts.
            existing_limit = manifest.get("tx_limit", 0)
            if tx_limit and (tx_limit > existing_limit or existing_limit == 0):
                manifest["tx_limit"] = tx_limit
            if tx_limit == 0 and existing_limit != 0:
                manifest["tx_limit"] = 0

        _atomic_write(path, json.dumps(manifest, indent=2, default=str))

        if discovered_utxos:
            save_utxos_to_store(discovered_utxos)
    except Exception as exc:
        logger.warning("save_address_trace_step failed: %s", exc)


class CachedAddrTrace(NamedTuple):
    """Partial address trace progress from manifest.

    Attributes:
        processed: Set of tx hashes successfully processed.
        failed: Set of tx hashes that failed (to re-query).
        total: Total number of tx hashes known for this address.
        tx_limit: The ``--tx-limit`` used when this cache was created
            (0 = no limit / all transactions).
        completed: Whether the trace completed normally.
    """
    processed: set[str]
    failed: set[str]
    total: int
    tx_limit: int
    completed: bool


def load_address_trace_partial(address: str) -> Optional[CachedAddrTrace]:
    """Load partial address trace progress.

    Returns None if no manifest exists (first-time query).
    """
    path = _addr_manifest_path(address)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text())
        return CachedAddrTrace(
            processed=set(manifest.get("tx_hashes_processed", [])),
            failed=set(manifest.get("tx_hashes_failed", [])),
            total=manifest.get("total_tx_count", 0),
            tx_limit=manifest.get("tx_limit", 0),
            completed=manifest.get("completed", False),
        )
    except (json.JSONDecodeError, OSError):
        return None


def finalize_address_trace(address: str) -> None:
    """Mark an address trace manifest as completed."""
    path = _addr_manifest_path(address)
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text())
        manifest["completed"] = True
        _atomic_write(path, json.dumps(manifest, indent=2, default=str))
    except Exception:
        pass


def _addr_result_to_dict(result: AddressTraceResult, cache_key: str) -> dict:
    return {
        "v": 2, "cache_key": cache_key,
        "target_address": result.target_address,
        "total_transactions": result.total_transactions,
        "error": result.error, "provider_name": result.provider_name,
        "addresses": [
            {"address": n.address, "address_type": n.address_type,
             "total_ada": n.total_ada, "net_ada": n.net_ada,
             "total_incoming_ada": n.total_incoming_ada,
             "total_outgoing_ada": n.total_outgoing_ada,
             "tx_count": n.tx_count,
             "is_cex": n.is_cex, "cex_name": n.cex_name, "is_target": n.is_target}
            for n in result.addresses
        ],
        "edges": [
            {"source": e.source, "target": e.target,
             "tx_hashes": e.tx_hashes, "interaction_count": e.interaction_count,
             "direction_relative_to_target": e.direction_relative_to_target}
            for e in result.edges
        ],
    }


def _addr_result_from_dict(data: dict, target_address: str) -> Optional[AddressTraceResult]:
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
            )
            for n in data.get("addresses", [])
        ]
        edges = [
            AddressInteractionEdge(
                source=e["source"], target=e["target"],
                tx_hashes=e.get("tx_hashes", []),
                interaction_count=e.get("interaction_count", len(e.get("tx_hashes", []))),
                direction_relative_to_target=e.get("direction_relative_to_target", "unknown"),
            )
            for e in data.get("edges", [])
        ]
        return AddressTraceResult(
            target_address=target_address,
            addresses=addresses, edges=edges,
            total_transactions=data.get("total_transactions", data.get("transactions_examined", 0)),
            error=data.get("error"),
            provider_name=data.get("provider_name", ""),
        )
    except Exception as e:
        logger.warning("Failed to parse cached address trace: %s", e)
        return None


def save_address_trace(result: AddressTraceResult, tx_limit: int = 0) -> str:
    """Save v2 address trace snapshot.

    The cache key includes ``tx_limit`` so different limits produce
    different snapshot files — just like UTXO trace's depth-based keys.
    """
    _ensure_dirs()
    key = _addr_cache_key(result.target_address)
    suffix = f"_{tx_limit}" if tx_limit else ""
    file_key = f"{key}{suffix}"
    index = _load_addr_index()
    index[file_key] = {
        "target_address": result.target_address[:40],
        "addresses": len(result.addresses),
        "edges": len(result.edges),
        "tx_limit": tx_limit,
        "created_at": time.time(),
    }
    _save_addr_index(index)
    data = _addr_result_to_dict(result, key)
    data["tx_limit"] = tx_limit
    _atomic_write(TRACES_DIR / f"{file_key}.json", json.dumps(data, indent=2, default=str))
    return key


def load_address_trace(address: str, tx_limit: int = 0) -> Optional[AddressTraceResult]:
    """Load a v2 address trace snapshot.

    Args:
        address: The target address.
        tx_limit: Required minimum ``--tx-limit``. If the cached snapshot
            was created with a smaller limit, it won't be loaded
            (caller should extend instead). 0 = match any limit.
    """
    key = _addr_cache_key(address)
    # Try exact-tx_limit file first, then fall back to no-limit
    candidates = [f"{key}_{tx_limit}"] if tx_limit else []
    candidates.append(key)
    for file_key in candidates:
        path = TRACES_DIR / f"{file_key}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                # Check the stored tx_limit matches expectations
                stored_limit = data.get("tx_limit", 0)
                if tx_limit and stored_limit and stored_limit < tx_limit:
                    continue  # insufficient coverage — skip
                return _addr_result_from_dict(data, address)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load cached address trace: %s", e)
                continue
    return None


def has_address_trace(address: str) -> bool:
    return (TRACES_DIR / f"{_addr_cache_key(address)}.json").exists()


def _load_addr_index() -> dict:
    try:
        return json.loads(ADDR_INDEX_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_addr_index(index: dict) -> None:
    _ensure_dirs()
    _atomic_write(ADDR_INDEX_FILE, json.dumps(index, indent=2, default=str))


# ---------------------------------------------------------------------------
# visualization state persistence
# ---------------------------------------------------------------------------

def save_viz_state(cache_key: str, state: dict) -> None:
    _ensure_dirs()
    _atomic_write(VIZ_DIR / f"{cache_key}.json", json.dumps(state, indent=2, default=str))


def load_viz_state(cache_key: str) -> dict:
    path = VIZ_DIR / f"{cache_key}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ===================================================================
# CROSS-CACHE: Save discovered UTXOs to global store
# ===================================================================

def save_utxos_to_store(utxos: list[UTxONode], edges: Optional[list[tuple[str, str, str]]] = None) -> None:
    """Save discovered UTXOs and edges to the global store.

    This is the cross-cache bridge: address traces call this to share
    UTXO data with the UTXO trace cache (and vice versa).

    Args:
        utxos: List of UTxONode objects to save
        edges: Optional list of (source, target, direction) tuples
    """
    if not utxos and not edges:
        return
    store = _load_store()
    for node in utxos:
        if node and node.address:
            store.setdefault("nodes", {})[node.id] = _node_to_dict(node)
    if edges:
        for source, target, direction in edges:
            add_edge_to_store(source, target, direction, store)
    _save_store(store)
