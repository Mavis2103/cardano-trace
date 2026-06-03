"""Forward UTXO tracing — edge-based dedup for diamond patterns.

Requires kupmios provider (for get_spent_utxos).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncGenerator, Optional

from ..cache import save_transaction
from ..models import OutRef, TraceStep, UTxONode
from ..providers.base import Provider


async def trace_forward(
    provider: Provider,
    start_out_ref: OutRef,
    max_depth: int = 5,
    timeout_per_fetch: float = 15.0,
    cached_nodes: Optional[dict[str, UTxONode]] = None,
    cached_outputs: Optional[dict[str, list[str]]] = None,
) -> AsyncGenerator[TraceStep, None]:
    """Async-generator that walks forward through spent outputs.

    Uses edge-based deduplication so diamond patterns preserve all branches.
    Supports kupmios (primary), blockfrost, and koios providers.
    """
    if provider.provider_type not in ("kupmios", "blockfrost", "koios"):
        yield TraceStep(
            out_ref=start_out_ref,
            direction="forward",
            depth=0,
            error=(
                f"Forward tracing requires 'kupmios', 'blockfrost', or 'koios' "
                f"provider (got '{provider.provider_type}')"
            ),
        )
        return

    visited: set[str] = set()
    seen_edges: set[str] = set()
    spent_cache: dict[str, list[OutRef]] = {}  # per-call address→spent cache
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
            direction="forward",
            depth=depth,
            parent_out_ref=parent_out_ref,
        )
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

        # Find transactions that spent any output going to this address
        address = step.utxo.address
        if address in spent_cache:
            spent_refs = spent_cache[address]
        elif cached_outputs and address in cached_outputs:
            spent_refs = [
                OutRef(
                    tx_hash=node_id.rsplit(":", 1)[0],
                    output_index=int(node_id.rsplit(":", 1)[1]),
                )
                for node_id in cached_outputs[address]
            ]
            spent_cache[address] = spent_refs
        else:
            try:
                spent_refs = await asyncio.wait_for(
                    provider.get_spent_utxos(address),
                    timeout=timeout_per_fetch,
                )
                spent_cache[address] = spent_refs
            except asyncio.TimeoutError:
                yield TraceStep(
                    out_ref=out_ref,
                    direction="forward",
                    depth=depth,
                    error=f"Timeout fetching spent for {address[:20]}...",
                )
                continue
            except NotImplementedError as e:
                yield TraceStep(
                    out_ref=out_ref,
                    direction="forward",
                    depth=depth,
                    error=str(e),
                )
                continue
            except Exception as e:
                yield TraceStep(
                    out_ref=out_ref,
                    direction="forward",
                    depth=depth,
                    error=f"{type(e).__name__}: {e}",
                )
                continue

        followed_tx_hashes: set[str] = set()
        for sref in spent_refs:
            spending_tx_hash = sref.tx_hash
            if not spending_tx_hash:
                continue
            if spending_tx_hash in followed_tx_hashes:
                continue
            followed_tx_hashes.add(spending_tx_hash)

            try:
                tx_data = await asyncio.wait_for(
                    provider.get_transaction_utxos(spending_tx_hash),
                    timeout=timeout_per_fetch,
                )
            except Exception as e:
                yield TraceStep(
                    out_ref=sref,
                    direction="forward",
                    depth=depth,
                    error=f"{type(e).__name__}: {e}",
                )
                continue

            # Cross-cache: save tx data so any trace type can reuse it
            save_transaction(spending_tx_hash, tx_data)

            for out_node in tx_data.get("outputs") or []:
                next_ref = out_node.out_ref
                if next_ref.node_id() in visited:
                    continue
                edge_id = f"{out_ref.node_id()}->{next_ref.node_id()}"
                if edge_id not in seen_edges:
                    seen_edges.add(edge_id)
                    queue.append((next_ref, depth + 1, out_ref))
