"""Forward UTXO tracing — UTXO-precise descendant walk.

Forward tracing answers "where did this specific UTXO's value go?". A UTXO is
consumed by *exactly one* transaction; the value then commingles into all of
that transaction's outputs. So for each node we:

  1. find the single transaction that spent this exact ``tx_hash:index``
     (via the provider's UTXO-precise spend map), and
  2. enqueue that spending transaction's outputs as the depth+1 descendants.

A UTXO with no spender is *unspent* — a terminal leaf. This is fundamentally
different from the old address-based walk, which followed every transaction that
ever touched the address and mis-read provider semantics (producing backward /
in-circles edges on blockfrost & koios).

Supported providers: kupmios (single-call precise), blockfrost, koios.
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
    cached_spend_map: Optional[dict[str, str]] = None,
) -> AsyncGenerator[TraceStep, None]:
    """Async-generator that walks forward through the spending transaction.

    Args:
        cached_nodes: ``node_id -> UTxONode`` already known (skip provider fetch).
        cached_spend_map: ``consumed node_id -> spending tx_hash`` already known
            (skip the provider spend-map lookup; only nodes missing here hit the
            provider). This is how smaller/repeat-depth re-traces avoid provider
            calls and larger-depth re-traces reuse prior work.
    """
    # Gate on the forward CAPABILITY, not provider_type — wrapper providers
    # (rotating / fallback) report the capability of what they wrap, so they
    # must not be excluded just because their type is "rotating"/"fallback".
    if not getattr(provider, "supports_forward", False):
        yield TraceStep(
            out_ref=start_out_ref,
            direction="forward",
            depth=0,
            error=(
                f"Forward tracing requires a kupmios, blockfrost, or koios "
                f"provider (got '{provider.provider_type}')"
            ),
        )
        return

    cached_spend_map = cached_spend_map or {}
    visited: set[str] = set()
    seen_edges: set[str] = set()
    # per-call address -> spend_map cache (avoids re-scanning an address)
    addr_spend_cache: dict[str, dict[str, str]] = {}
    # per-call spending tx -> outputs cache (a tx is followed once)
    tx_outputs_cache: dict[str, list[UTxONode]] = {}
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
        if depth >= max_depth:
            continue  # leaf at the depth cap — don't fetch its spender

        # 1) Which transaction spent THIS exact UTXO?
        spending_tx_hash = cached_spend_map.get(node_id)
        if spending_tx_hash is None:
            address = step.utxo.address
            spend_map = addr_spend_cache.get(address)
            if spend_map is None:
                try:
                    spend_map = await asyncio.wait_for(
                        provider.get_address_spend_map(address),
                        timeout=timeout_per_fetch,
                    )
                except asyncio.TimeoutError:
                    yield TraceStep(
                        out_ref=out_ref,
                        direction="forward",
                        depth=depth,
                        error=f"Timeout fetching spend map for {address[:20]}...",
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
                addr_spend_cache[address] = spend_map
            spending_tx_hash = spend_map.get(node_id)

        if not spending_tx_hash:
            continue  # UTXO is unspent → terminal leaf

        # 2) Follow the spending transaction's outputs (value commingles).
        outputs = tx_outputs_cache.get(spending_tx_hash)
        if outputs is None:
            try:
                tx_data = await asyncio.wait_for(
                    provider.get_transaction_utxos(spending_tx_hash),
                    timeout=timeout_per_fetch,
                )
            except Exception as e:
                yield TraceStep(
                    out_ref=OutRef(spending_tx_hash, 0),
                    direction="forward",
                    depth=depth + 1,
                    error=f"{type(e).__name__}: {e}",
                )
                continue
            # Cross-cache: save tx data so any trace type can reuse it
            save_transaction(spending_tx_hash, tx_data)
            outputs = tx_data.get("outputs") or []
            tx_outputs_cache[spending_tx_hash] = outputs

        for out_node in outputs:
            next_ref = out_node.out_ref
            if next_ref.node_id() in visited:
                continue
            edge_id = f"{out_ref.node_id()}->{next_ref.node_id()}"
            if edge_id not in seen_edges:
                seen_edges.add(edge_id)
                # pre-seed node data we already fetched as the tx output
                if cached_nodes is not None:
                    cached_nodes.setdefault(next_ref.node_id(), out_node)
                queue.append((next_ref, depth + 1, out_ref))
