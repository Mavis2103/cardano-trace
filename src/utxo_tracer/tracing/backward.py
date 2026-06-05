"""Backward UTXO tracing — edge-based dedup for diamond patterns."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncGenerator, Optional

from ..cache import save_transaction
from ..models import OutRef, TraceStep, UTxONode
from ..providers.base import Provider


async def trace_backward(
    provider: Provider,
    start_out_ref: OutRef,
    max_depth: int = 5,
    timeout_per_fetch: float = 15.0,
    cached_nodes: Optional[dict[str, UTxONode]] = None,
    cached_inputs: Optional[dict[str, list[str]]] = None,
) -> AsyncGenerator[TraceStep, None]:
    """Async-generator that walks backwards from start_out_ref through tx inputs.

    Uses edge-based deduplication (not node-based) so diamond patterns
    preserve all branches:

        X
       / \\
      A   B       Both A→X and B→X edges are kept.
       \\ /
        Y
    """
    visited: set[str] = set()
    seen_edges: set[str] = set()
    queue: deque[tuple[OutRef, int, Optional[OutRef]]] = deque(
        [(start_out_ref, 0, None)]
    )

    while queue:
        out_ref, depth, parent_out_ref = queue.popleft()
        node_id = out_ref.node_id()
        if depth > max_depth or node_id in visited:
            continue
        visited.add(node_id)

        step = TraceStep(
            out_ref=out_ref,
            direction="backward",
            depth=depth,
            parent_out_ref=parent_out_ref,
        )
        # Check cache before provider query
        if cached_nodes and node_id in cached_nodes:
            step.utxo = cached_nodes[node_id]
        else:
            try:
                step.utxo = await asyncio.wait_for(
                    provider.get_utxo_by_out_ref(out_ref), timeout=timeout_per_fetch
                )
            except asyncio.TimeoutError:
                step.error = f"Timeout fetching {out_ref}"
            except Exception as e:
                step.error = f"{type(e).__name__}: {e}"
        yield step

        if step.error or step.utxo is None:
            continue
        if depth >= max_depth:
            continue  # leaf at the depth cap — don't fetch its inputs/tx

        # Best-effort input_utxos pre-cache: only when cached_inputs lists
        # source nodes we DON'T already have data for. Without this guard a
        # full cache hit (smaller-depth re-trace, every input already cached)
        # would still fire one provider tx-fetch per node — defeating the
        # "smaller depth = no provider" requirement.
        _missing_inputs = (
            cached_nodes is not None
            and cached_inputs
            and out_ref.node_id() in cached_inputs
            and any(
                src_id not in cached_nodes
                for src_id in cached_inputs[out_ref.node_id()]
            )
        )
        if _missing_inputs:
            try:
                tx_data = await asyncio.wait_for(
                    provider.get_transaction_utxos(out_ref.tx_hash),
                    timeout=timeout_per_fetch,
                )
                input_utxos: dict = tx_data.get("input_utxos", {})
                for src_id in cached_inputs[out_ref.node_id()]:
                    if src_id in input_utxos and src_id not in cached_nodes:
                        cached_nodes[src_id] = input_utxos[src_id]
                # Cross-cache: save tx data so any trace type can reuse it
                save_transaction(out_ref.tx_hash, tx_data)
            except Exception:
                pass  # best-effort — fall through to individual fetches

        # Use cached inputs if available (skip provider for edge enqueueing)
        if cached_inputs and out_ref.node_id() in cached_inputs:
            for src_id in cached_inputs[out_ref.node_id()]:
                edge_id = f"{out_ref.node_id()}->{src_id}"
                if edge_id not in seen_edges:
                    seen_edges.add(edge_id)
                    parts = src_id.rsplit(":", 1)
                    queue.append(
                        (
                            OutRef(parts[0], int(parts[1])),
                            depth + 1,
                            out_ref,
                        )
                    )
        else:
            try:
                tx_data = await asyncio.wait_for(
                    provider.get_transaction_utxos(out_ref.tx_hash),
                    timeout=timeout_per_fetch,
                )
                # Pre-cache any input UTXO data returned by the provider
                # (avoids separate get_utxo_by_out_ref() calls when the API
                # already returned address + amount alongside the input refs)
                input_utxos: dict = tx_data.get("input_utxos", {})
                for input_ref in tx_data.get("inputs") or []:
                    nid = input_ref.node_id()
                    if nid in input_utxos and cached_nodes is not None:
                        cached_nodes[nid] = input_utxos[nid]
                    edge_id = f"{out_ref.node_id()}->{nid}"
                    if edge_id not in seen_edges:
                        seen_edges.add(edge_id)
                        queue.append((input_ref, depth + 1, out_ref))
                # Cross-cache: save tx data so any trace type can reuse it
                save_transaction(out_ref.tx_hash, tx_data)
            except asyncio.TimeoutError:
                yield TraceStep(
                    out_ref=out_ref,
                    direction="backward",
                    depth=depth,
                    error=f"Timeout fetching tx {out_ref.tx_hash}",
                )
            except Exception as e:
                yield TraceStep(
                    out_ref=out_ref,
                    direction="backward",
                    depth=depth,
                    error=f"{type(e).__name__}: {e}",
                )
