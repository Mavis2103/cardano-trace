"""Address-level interaction tracing with multi-hop BFS support.

Given a Cardano address, finds ALL other addresses connected through shared
transactions — either direct (depth=1) or multi-hop (depth=N).

Algorithm
=========
BFS queue over addresses::

    queue = [(target_address, depth=0)]
    while queue:
        addr, depth = queue.popleft()
        if depth >= max_depth: continue
        tx_hashes = get_address_transactions(addr)
        for each tx:
            input_addrs, output_addrs = get_transaction_utxos(tx_hash)
            record directed edges: input → output
            for each counterparty C (where C != addr):
                if C not visited and depth + 1 < max_depth:
                    queue.append((C, depth + 1))
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from typing import Awaitable, Callable, Optional

from ..cex.registry import identify_cex
from ..models import AddressInteractionEdge, AddressInteractionNode, AddressTraceResult, UTxONode
from ..providers.base import Provider
from ..utils import classify_address

logger = logging.getLogger(__name__)

_TIMEOUT_PER_FETCH = 15.0
_BATCH_SIZE = 20
_MAX_TX_LIMIT = 100_000
_MAX_ADDRESSES = 500  # hard safety cap


async def trace_address_interactions(
    provider: Provider,
    target_address: str,
    max_depth: int = 1,
    tx_limit: Optional[int] = None,
    timeout_per_fetch: float = _TIMEOUT_PER_FETCH,
    progress_callback: Optional[Callable[[int, int], Awaitable[None]]] = None,
    skip_tx_hashes: Optional[dict[str, set[str]]] = None,
    step_callback: Optional[Callable[[str, str, Optional[str], int], None]] = None,
) -> AddressTraceResult:
    """Trace addresses connected to *target_address*, up to *max_depth* hops.

    Each hop fetches ALL transactions for an address (paginated) and records
    directed edges between input addresses and output addresses, filtering
    to only keep edges where one endpoint is the address being expanded.

    Parameters
    ----------
    provider:
        Data provider with address-tx lookup capability.
    target_address:
        The Cardano address to trace.
    max_depth:
        Maximum BFS depth. ``1`` = direct interactions only (default).
    tx_limit:
        Cap on transactions per address-level. ``None`` = no limit.
    timeout_per_fetch:
        Per-API-call timeout in seconds.
    progress_callback:
        Async callback ``(completed, total)`` called during concurrent tx
        fetch phase for each depth level.
    skip_tx_hashes:
        ``{address: set(tx_hashes)}`` — tx hashes to skip (for cache extension).
    step_callback:
        Called after each tx is processed with
        ``(source_address, tx_hash, error_or_None, depth)``.
        Use for per-step cache saving.
    """
    provider_name = getattr(provider, "current_provider", "") or getattr(
        provider, "provider_type", ""
    )

    # ── BFS queue ────────────────────────────────────────────────────────
    queue: deque[tuple[str, int]] = deque([(target_address, 0)])
    visited_addresses: set[str] = {target_address}

    # Accumulators
    all_edges: list[tuple[str, str, str, int]] = []  # (source, target, tx_hash, depth)
    addr_tx_map: dict[str, set[str]] = defaultdict(set)
    addr_net_ada: dict[str, float] = defaultdict(float)
    addr_gross_ada: dict[str, float] = defaultdict(float)
    addr_incoming_ada: dict[str, float] = defaultdict(float)
    addr_outgoing_ada: dict[str, float] = defaultdict(float)
    addr_type: dict[str, str] = {target_address: classify_address(target_address).value}
    addr_depth: dict[str, int] = {target_address: 0}
    errors: list[str] = []
    discovered_utxos: list[UTxONode] = []
    total_tx_processed = 0

    # ── BFS loop ─────────────────────────────────────────────────────────
    while queue:
        current_addr, current_depth = queue.popleft()

        if current_depth >= max_depth:
            continue

        # ── Step 1: Fetch ALL tx hashes for this address ────────────────
        try:
            tx_hashes = await asyncio.wait_for(
                provider.get_address_transactions(current_addr),
                timeout=timeout_per_fetch * 5,
            )
        except NotImplementedError as e:
            # Provider can't do address-tx lookup — skip this address
            errors.append(f"{current_addr[:16]}…: {e}")
            continue
        except Exception as e:
            errors.append(f"{current_addr[:16]}…: Failed tx fetch: {type(e).__name__}: {e}")
            continue

        if not tx_hashes:
            continue

        # Apply tx_limit per address level
        total_fetched = len(tx_hashes)
        if tx_limit is not None:
            tx_hashes = tx_hashes[:tx_limit]
        elif total_fetched > _MAX_TX_LIMIT:
            tx_hashes = tx_hashes[:_MAX_TX_LIMIT]
            logger.warning(
                "Address %s has %d transactions — capped at %d per level",
                current_addr[:20], total_fetched, _MAX_TX_LIMIT,
            )

        # Remove already-processed tx hashes (cache extension)
        if skip_tx_hashes and current_addr in skip_tx_hashes:
            tx_hashes = [h for h in tx_hashes if h not in skip_tx_hashes[current_addr]]

        effective_count = len(tx_hashes)
        if effective_count == 0:
            continue

        total_tx_processed += effective_count

        # Hard cap on total addresses
        if len(visited_addresses) >= _MAX_ADDRESSES and current_depth > 0:
            logger.warning("Reached %d address limit — stopping expansion", _MAX_ADDRESSES)
            break

        # ── Step 2: Fetch tx details — try batch, fall back to concurrent ─
        sem = asyncio.Semaphore(10)

        async def _examine_tx(tx_hash: str) -> None:
            """Fetch one tx and record edges + ADA flow."""
            nonlocal discovered_utxos
            async with sem:
                tx_error: Optional[str] = None
                try:
                    tx_data = await asyncio.wait_for(
                        provider.get_transaction_utxos(tx_hash),
                        timeout=timeout_per_fetch,
                    )
                except Exception as e:
                    tx_error = f"{type(e).__name__}: {e}"
                    errors.append(f"{tx_hash[:16]}…: {tx_error}")
                    if step_callback:
                        step_callback(current_addr, tx_hash, tx_error, current_depth)
                    return

                # CROSS-CACHE
                _collect_utxos(tx_data, discovered_utxos)

                # Extract input/output addresses with ADA
                input_addrs = _extract_input_addrs(tx_data)
                output_addrs = _extract_output_addrs(tx_data)

                # Record directed edges
                _record_tx_edges(
                    tx_hash, current_addr, current_depth,
                    input_addrs, output_addrs,
                    all_edges, addr_tx_map,
                    addr_net_ada, addr_gross_ada,
                    addr_incoming_ada, addr_outgoing_ada,
                    addr_type, addr_depth, queue, visited_addresses,
                    max_depth,
                )

                if step_callback:
                    step_callback(current_addr, tx_hash, None, current_depth)

        # ── Try batch API first ──────────────────────────────────────────
        try:
            batch_results = await asyncio.wait_for(
                provider.get_transactions_utxos(tx_hashes),
                timeout=timeout_per_fetch * (_BATCH_SIZE + 2),
            )
            for tx_hash, tx_data in zip(tx_hashes, batch_results):
                _process_tx_data_static(
                    tx_hash, current_addr, current_depth,
                    tx_data,
                    all_edges, addr_tx_map,
                    addr_net_ada, addr_gross_ada,
                    addr_incoming_ada, addr_outgoing_ada,
                    addr_type, addr_depth, queue, visited_addresses,
                    max_depth, errors, discovered_utxos,
                )
                if step_callback:
                    has_data = bool(tx_data.get("inputs") or tx_data.get("outputs"))
                    tx_err = None if has_data else "empty result"
                    step_callback(current_addr, tx_hash, tx_err, current_depth)
        except (NotImplementedError, Exception) as exc:
            # Fall back to concurrent single-tx
            if not isinstance(exc, NotImplementedError):
                logger.debug(
                    "Batch failed for %s depth=%d: %s — fallback to concurrent",
                    current_addr[:16], current_depth, exc,
                )

            tasks = [asyncio.create_task(_examine_tx(tx_hash)) for tx_hash in tx_hashes]
            completed = 0
            pending: set[asyncio.Task] = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED,
                )
                completed += len(done)
                if progress_callback:
                    await progress_callback(completed, effective_count)

    # ── Step 3: Build result ─────────────────────────────────────────────

    # Collect all unique addresses
    all_addrs_set: set[str] = set()
    for s, t, _tx, _d in all_edges:
        all_addrs_set.add(s)
        all_addrs_set.add(t)
    all_addrs_set.add(target_address)

    seen: set[str] = set()
    nodes: list[AddressInteractionNode] = []
    for addr in sorted(all_addrs_set):
        if addr in seen:
            continue
        seen.add(addr)
        cex = identify_cex(addr)
        nodes.append(AddressInteractionNode(
            address=addr,
            address_type=addr_type.get(addr, classify_address(addr).value),
            total_ada=round(addr_gross_ada.get(addr, 0.0), 6),
            net_ada=round(addr_net_ada.get(addr, 0.0), 6),
            total_incoming_ada=round(addr_incoming_ada.get(addr, 0.0), 6),
            total_outgoing_ada=round(addr_outgoing_ada.get(addr, 0.0), 6),
            tx_count=len(addr_tx_map.get(addr, set())),
            is_cex=cex is not None,
            cex_name=cex.name if cex else "",
            is_target=(addr == target_address),
            depth=addr_depth.get(addr, 0),
        ))

    # Aggregate edges by (source, target) pair
    edge_map: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for in_addr, out_addr, tx_hash, depth in all_edges:
        edge_map[(in_addr, out_addr)].append((tx_hash, depth))

    edges: list[AddressInteractionEdge] = []
    for (source, target), tx_list in edge_map.items():
        direction = _compute_direction(source, target, target_address)
        min_depth = min(d for _, d in tx_list)
        edges.append(AddressInteractionEdge(
            source=source,
            target=target,
            tx_hashes=[h for h, _ in tx_list],
            interaction_count=len(tx_list),
            direction_relative_to_target=direction,
            source_depth=min_depth,
        ))

    result = AddressTraceResult(
        target_address=target_address,
        addresses=nodes,
        edges=edges,
        total_transactions=total_tx_processed,
        error=_format_errors(errors),
        provider_name=provider_name,
        max_depth=max_depth,
    )

    # CROSS-CACHE
    if discovered_utxos:
        try:
            from ..cache import save_utxos_to_store
            save_utxos_to_store(discovered_utxos)
        except Exception:
            logger.warning("Cross-cache save failed (non-critical)", exc_info=True)

    return result


# ── Internal helpers ──────────────────────────────────────────────────────


def _extract_input_addrs(tx_data: dict) -> dict[str, float]:
    """Extract {address: total_ada} from tx inputs."""
    result: dict[str, float] = {}
    for iutxo in tx_data.get("input_utxos", {}).values():
        if iutxo is None or not iutxo.address:
            continue
        result[iutxo.address] = result.get(iutxo.address, 0.0) + iutxo.ada
    return result


def _extract_output_addrs(tx_data: dict) -> dict[str, float]:
    """Extract {address: total_ada} from tx outputs."""
    result: dict[str, float] = {}
    for out in tx_data.get("outputs", []):
        if out is None or not out.address:
            continue
        result[out.address] = result.get(out.address, 0.0) + out.ada
    return result


def _collect_utxos(tx_data: dict, discovered: list[UTxONode]) -> None:
    """Cross-cache: collect all UTXOs from tx data."""
    for iutxo in tx_data.get("input_utxos", {}).values():
        if iutxo is not None and iutxo.address:
            discovered.append(iutxo)
    for out in tx_data.get("outputs", []):
        if out is not None and out.address:
            discovered.append(out)


def _record_tx_edges(
    tx_hash: str,
    current_addr: str,
    current_depth: int,
    input_addrs: dict[str, float],
    output_addrs: dict[str, float],
    all_edges: list[tuple[str, str, str, int]],
    addr_tx_map: dict[str, set[str]],
    addr_net_ada: dict[str, float],
    addr_gross_ada: dict[str, float],
    addr_incoming_ada: dict[str, float],
    addr_outgoing_ada: dict[str, float],
    addr_type: dict[str, str],
    addr_depth: dict[str, int],
    queue: deque[tuple[str, int]],
    visited_addresses: set[str],
    max_depth: int,
) -> None:
    """Record directed edges + ADA flow for one tx, queuing new addresses."""

    # Directed edges: input → output, keep only pairs where current_addr is one side
    for in_addr in input_addrs:
        for out_addr in output_addrs:
            if in_addr == out_addr:
                continue
            if in_addr == current_addr or out_addr == current_addr:
                all_edges.append((in_addr, out_addr, tx_hash, current_depth))

                # Track counterparty for BFS expansion
                other = out_addr if in_addr == current_addr else in_addr
                if other not in visited_addresses and current_depth + 1 < max_depth:
                    visited_addresses.add(other)
                    queue.append((other, current_depth + 1))

    # Update per-address data
    _update_addr_data(
        input_addrs, output_addrs, tx_hash,
        addr_tx_map, addr_net_ada, addr_gross_ada,
        addr_incoming_ada, addr_outgoing_ada,
        addr_type, addr_depth, current_depth,
    )


def _update_addr_data(
    input_addrs: dict[str, float],
    output_addrs: dict[str, float],
    tx_hash: str,
    addr_tx_map: dict[str, set[str]],
    addr_net_ada: dict[str, float],
    addr_gross_ada: dict[str, float],
    addr_incoming_ada: dict[str, float],
    addr_outgoing_ada: dict[str, float],
    addr_type: dict[str, str],
    addr_depth: dict[str, int],
    current_depth: int,
) -> None:
    """Update per-address accumulators for the given input/output addrs."""
    for addr, ada_val in input_addrs.items():
        if addr not in addr_type:
            addr_type[addr] = classify_address(addr).value
        if addr not in addr_depth:
            addr_depth[addr] = current_depth
        addr_net_ada[addr] -= ada_val
        addr_outgoing_ada[addr] += ada_val
        addr_gross_ada[addr] += ada_val
        addr_tx_map[addr].add(tx_hash)

    for addr, ada_val in output_addrs.items():
        if addr not in addr_type:
            addr_type[addr] = classify_address(addr).value
        if addr not in addr_depth:
            addr_depth[addr] = current_depth
        addr_net_ada[addr] += ada_val
        addr_incoming_ada[addr] += ada_val
        addr_gross_ada[addr] += ada_val
        addr_tx_map[addr].add(tx_hash)


def _process_tx_data_static(
    tx_hash: str,
    current_addr: str,
    current_depth: int,
    tx_data: dict,
    all_edges: list[tuple[str, str, str, int]],
    addr_tx_map: dict[str, set[str]],
    addr_net_ada: dict[str, float],
    addr_gross_ada: dict[str, float],
    addr_incoming_ada: dict[str, float],
    addr_outgoing_ada: dict[str, float],
    addr_type: dict[str, str],
    addr_depth: dict[str, int],
    queue: deque[tuple[str, int]],
    visited_addresses: set[str],
    max_depth: int,
    errors: list[str],
    discovered_utxos: list[UTxONode],
) -> None:
    """Process pre-fetched tx_data (batch path)."""
    _collect_utxos(tx_data, discovered_utxos)

    input_addrs = _extract_input_addrs(tx_data)
    output_addrs = _extract_output_addrs(tx_data)

    _record_tx_edges(
        tx_hash, current_addr, current_depth,
        input_addrs, output_addrs,
        all_edges, addr_tx_map,
        addr_net_ada, addr_gross_ada,
        addr_incoming_ada, addr_outgoing_ada,
        addr_type, addr_depth, queue, visited_addresses,
        max_depth,
    )


def _compute_direction(source: str, target: str, target_address: str) -> str:
    """Determine direction of interaction relative to target_address."""
    if source == target_address and target == target_address:
        return "both"
    if source == target_address:
        return "outgoing"
    if target == target_address:
        return "incoming"
    return "unknown"


def _format_errors(errors: list[str]) -> Optional[str]:
    if not errors:
        return None
    text = "; ".join(errors[:5])
    if len(errors) > 5:
        text += f" (+{len(errors) - 5} more)"
    return text
