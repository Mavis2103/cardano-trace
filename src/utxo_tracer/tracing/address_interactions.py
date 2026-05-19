"""Address-level interaction tracing.

Given a Cardano address, finds ALL other addresses that have interacted
with it through shared transactions, building a directed interaction graph
with net ADA flow information.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Optional

from ..cex.registry import identify_cex
from ..models import AddressInteractionEdge, AddressInteractionNode, AddressTraceResult, UTxONode
from ..providers.base import Provider
from ..utils import classify_address

logger = logging.getLogger(__name__)

_TIMEOUT_PER_FETCH = 15.0
_BATCH_SIZE = 20  # number of tx details to fetch concurrently
_MAX_TX_LIMIT = 100_000  # hard safety cap against runaway queries


async def trace_address_interactions(
    provider: Provider,
    target_address: str,
    tx_limit: Optional[int] = None,
    timeout_per_fetch: float = _TIMEOUT_PER_FETCH,
    progress_callback: Optional[Callable[[int, int], Awaitable[None]]] = None,
    skip_tx_hashes: Optional[set[str]] = None,
    step_callback: Optional[Callable[[str, Optional[str]], None]] = None,
) -> AddressTraceResult:
    """Trace ALL addresses that have interacted with *target_address*.

    Steps:
      1. Fetch ALL transaction hashes involving *target_address* (paginated).
      2. For each transaction, fetch input/output UTXOs (batched when possible).
      3. Extract directed edges: input address(es) → output address(es).
      4. Track net ADA flow: input = negative, output = positive per address.
      5. Build result with deduplicated address nodes and directed edges.

    Parameters
    ----------
    provider:
        Data provider with address-tx lookup capability.
    target_address:
        The Cardano address to trace.
    tx_limit:
        Optional cap on transactions to examine. ``None`` = no limit
        (fetches all pages until the API returns empty).
    timeout_per_fetch:
        Per-API-call timeout in seconds.
    progress_callback:
        Async callback ``(completed, total)`` called periodically during
        the transaction-detail fetch phase.
    skip_tx_hashes:
        Set of tx hashes already processed (from partial cache). These
        are skipped to avoid re-querying.
    step_callback:
        Called synchronously after each tx hash is processed with
        ``(tx_hash, error_or_None)``. Use for per-step cache saving
        (e.g. :func:`cache.save_address_trace_step`).

    Returns
    -------
    AddressTraceResult with all discovered addresses, directed edges,
    and net ADA flow.
    """
    provider_name = getattr(provider, "current_provider", "") or getattr(
        provider, "provider_type", ""
    )

    # ── Step 1: Fetch ALL transaction hashes (paginated, unlimited) ──────
    try:
        tx_hashes = await asyncio.wait_for(
            provider.get_address_transactions(target_address),
            timeout=timeout_per_fetch * 5,
        )
    except NotImplementedError as e:
        return AddressTraceResult(
            target_address=target_address,
            addresses=[
                AddressInteractionNode(
                    address=target_address,
                    address_type=classify_address(target_address).value,
                    is_target=True,
                )
            ],
            edges=[],
            total_transactions=0,
            error=str(e),
            provider_name=provider_name,
        )
    except Exception as e:
        return AddressTraceResult(
            target_address=target_address,
            addresses=[
                AddressInteractionNode(
                    address=target_address,
                    address_type=classify_address(target_address).value,
                    is_target=True,
                )
            ],
            edges=[],
            total_transactions=0,
            error=f"Failed to fetch transactions: {type(e).__name__}: {e}",
            provider_name=provider_name,
        )

    if not tx_hashes:
        return AddressTraceResult(
            target_address=target_address,
            addresses=[
                AddressInteractionNode(
                    address=target_address,
                    address_type=classify_address(target_address).value,
                    is_target=True,
                )
            ],
            edges=[],
            total_transactions=0,
            provider_name=provider_name,
        )

    # Safety cap: never process more than _MAX_TX_LIMIT tx details
    total_tx = len(tx_hashes)
    if tx_limit is not None:
        tx_hashes = tx_hashes[:tx_limit]
    elif total_tx > _MAX_TX_LIMIT:
        tx_hashes = tx_hashes[:_MAX_TX_LIMIT]
        logger.warning(
            "Address %s has %d transactions — capped at %d for safety. "
            "Set tx_limit explicitly to adjust.",
            target_address[:20], total_tx, _MAX_TX_LIMIT,
        )

    effective_count = len(tx_hashes)

    # Skip already-processed tx hashes (from partial cache extension)
    if skip_tx_hashes:
        before = effective_count
        tx_hashes = [th for th in tx_hashes if th not in skip_tx_hashes]
        skipped = before - len(tx_hashes)
        effective_count = len(tx_hashes)
        if skipped:
            logger.info(
                "Address %s: skipped %d already-cached tx(s) (%d remaining)",
                target_address[:20], skipped, effective_count,
            )

    if not tx_hashes:
        # All txs were cached — build from store instead of empty result
        # (handled by caller; return empty is fine too)
        pass

    # ── Step 2: Fetch tx details ────────────────────────────────────────

    # Directed edges: (from_addr, to_addr, tx_hash)
    directed_edges: list[tuple[str, str, str]] = []
    # addr_tx_map: address → set of tx hashes
    addr_tx_map: dict[str, set[str]] = defaultdict(set)
    # addr_net_ada: address → net ADA flow (input=negative, output=positive)
    addr_net_ada: dict[str, float] = defaultdict(float)
    # addr_gross_ada: address → total ADA seen (absolute value, for display)
    addr_gross_ada: dict[str, float] = defaultdict(float)
    # addr_incoming_ada / addr_outgoing_ada: breakdown
    addr_incoming_ada: dict[str, float] = defaultdict(float)
    addr_outgoing_ada: dict[str, float] = defaultdict(float)
    # addr_type: address → classification string
    addr_type: dict[str, str] = {}
    errors: list[str] = []

    # CROSS-CACHE: collect UTXOs to share with UTXO trace cache
    discovered_utxos: list[UTxONode] = []

    sem = asyncio.Semaphore(10)

    async def examine_tx(tx_hash: str) -> None:
        """Fetch one tx and record directed edges + net ADA flow."""
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
                    step_callback(tx_hash, tx_error)
                return

            # CROSS-CACHE: collect all UTXOs from this tx
            for iutxo in tx_data.get("input_utxos", {}).values():
                if iutxo is not None and iutxo.address:
                    discovered_utxos.append(iutxo)
            for out in tx_data.get("outputs", []):
                if out is not None and out.address:
                    discovered_utxos.append(out)

            # Collect input addresses (spenders) with their ADA values
            input_addrs: dict[str, float] = {}
            for inp_id, iutxo in tx_data.get("input_utxos", {}).items():
                if iutxo is None or not iutxo.address:
                    continue
                addr = iutxo.address
                input_addrs[addr] = input_addrs.get(addr, 0.0) + iutxo.ada

            # Collect output addresses (receivers) with their ADA values
            output_addrs: dict[str, float] = {}
            for out in tx_data.get("outputs", []):
                if out is None or not out.address:
                    continue
                addr = out.address
                output_addrs[addr] = output_addrs.get(addr, 0.0) + out.ada

            # ── Directed edges: every input → every output ─────
            for in_addr, in_ada in input_addrs.items():
                for out_addr, out_ada in output_addrs.items():
                    if in_addr == out_addr:
                        continue  # skip self-interaction (change)
                    directed_edges.append((in_addr, out_addr, tx_hash))

            # ── Net ADA: input = negative, output = positive ──
            for addr, ada_val in input_addrs.items():
                _record_addr(addr, classify_address(addr).value)
                addr_net_ada[addr] -= ada_val
                addr_outgoing_ada[addr] += ada_val
                addr_gross_ada[addr] += ada_val
                addr_tx_map[addr].add(tx_hash)

            for addr, ada_val in output_addrs.items():
                _record_addr(addr, classify_address(addr).value)
                addr_net_ada[addr] += ada_val
                addr_incoming_ada[addr] += ada_val
                addr_gross_ada[addr] += ada_val
                addr_tx_map[addr].add(tx_hash)

            if step_callback:
                step_callback(tx_hash, None)

    def _record_addr(addr: str, addr_type_val: str) -> None:
        if addr not in addr_type:
            addr_type[addr] = addr_type_val

    # ── Try batch API first, fall back to concurrent single-tx ──────
    try:
        # Try batch: send all tx_hashes at once (Koios supports this)
        batch_results = await asyncio.wait_for(
            provider.get_transactions_utxos(tx_hashes),
            timeout=timeout_per_fetch * (_BATCH_SIZE + 2),
        )
        # Parse batch results using the same examine logic
        for tx_hash, tx_data in zip(tx_hashes, batch_results):
            await _process_tx_data(
                tx_hash, tx_data, directed_edges, addr_tx_map,
                addr_net_ada, addr_gross_ada, addr_incoming_ada,
                addr_outgoing_ada, addr_type, errors, discovered_utxos,
            )
            if step_callback:
                # Check if the tx had an error in the empty result
                has_data = bool(tx_data.get("inputs") or tx_data.get("outputs"))
                tx_err = None if has_data else "empty result"
                step_callback(tx_hash, tx_err)
    except (NotImplementedError, Exception):
        # Fall back to concurrent single-tx fetching
        _LOGGER = logging.getLogger(__name__)
        _LOGGER.debug(
            "Batch get_transactions_utxos not available, falling back to "
            "concurrent single-tx fetching for %d tx(s)",
            len(tx_hashes),
        )
        tasks = [asyncio.create_task(examine_tx(tx_hash)) for tx_hash in tx_hashes]

        # Process with streaming progress
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
    # Filter to only direct interactions with target_address.
    # The loop above creates edges between ALL input→output pairs in a
    # transaction, including pairs where neither address is the target.
    # This brings in "bystander" addresses (copayers, change, other
    # recipients) that the user didn't directly interact with.
    directed_edges = [
        (s, t, tx) for s, t, tx in directed_edges
        if s == target_address or t == target_address
    ]

    # Collect all addresses that appear in edges
    all_addresses: set[str] = set()
    for in_addr, out_addr, _ in directed_edges:
        all_addresses.add(in_addr)
        all_addresses.add(out_addr)
    all_addresses.add(target_address)

    # Build node list
    seen_addrs: set[str] = set()
    nodes: list[AddressInteractionNode] = []
    for addr in sorted(all_addresses):
        if addr in seen_addrs:
            continue
        seen_addrs.add(addr)
        cex = identify_cex(addr)
        nodes.append(
            AddressInteractionNode(
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
            )
        )

    # Build directed edges with direction info
    # Aggregate by (source, target) pair
    edge_map: dict[tuple[str, str], list[str]] = defaultdict(list)
    for in_addr, out_addr, tx_hash in directed_edges:
        # Store as (input, output) so source=spender, target=receiver
        pair = (in_addr, out_addr)
        edge_map[pair].append(tx_hash)

    edges: list[AddressInteractionEdge] = []
    for (source, target), tx_list in edge_map.items():
        # Determine direction relative to target_address
        direction = _compute_direction(source, target, target_address)
        edges.append(
            AddressInteractionEdge(
                source=source,
                target=target,
                tx_hashes=tx_list,
                interaction_count=len(tx_list),
                direction_relative_to_target=direction,
            )
        )

    result = AddressTraceResult(
        target_address=target_address,
        addresses=nodes,
        edges=edges,
        total_transactions=effective_count,
        error=_format_errors(errors),
        provider_name=provider_name,
    )

    # CROSS-CACHE: save discovered UTXOs to global store
    if discovered_utxos:
        try:
            from ..cache import save_utxos_to_store
            save_utxos_to_store(discovered_utxos)
        except Exception:
            logger.warning("Cross-cache save failed (non-critical)", exc_info=True)

    return result


async def _process_tx_data(
    tx_hash: str,
    tx_data: dict,
    directed_edges: list[tuple[str, str, str]],
    addr_tx_map: dict[str, set[str]],
    addr_net_ada: dict[str, float],
    addr_gross_ada: dict[str, float],
    addr_incoming_ada: dict[str, float],
    addr_outgoing_ada: dict[str, float],
    addr_type: dict[str, str],
    errors: list[str],
    discovered_utxos: list[UTxONode],
) -> None:
    """Process a single tx_data dict (from batch or single fetch)."""
    # CROSS-CACHE
    for iutxo in tx_data.get("input_utxos", {}).values():
        if iutxo is not None and iutxo.address:
            discovered_utxos.append(iutxo)
    for out in tx_data.get("outputs", []):
        if out is not None and out.address:
            discovered_utxos.append(out)

    # Input addresses
    input_addrs: dict[str, float] = {}
    for iutxo in tx_data.get("input_utxos", {}).values():
        if iutxo is None or not iutxo.address:
            continue
        input_addrs[iutxo.address] = input_addrs.get(iutxo.address, 0.0) + iutxo.ada

    # Output addresses
    output_addrs: dict[str, float] = {}
    for out in tx_data.get("outputs", []):
        if out is None or not out.address:
            continue
        output_addrs[out.address] = output_addrs.get(out.address, 0.0) + out.ada

    # Directed edges
    for in_addr in input_addrs:
        for out_addr in output_addrs:
            if in_addr == out_addr:
                continue
            directed_edges.append((in_addr, out_addr, tx_hash))

    # Net ADA
    for addr, ada_val in input_addrs.items():
        if addr not in addr_type:
            addr_type[addr] = classify_address(addr).value
        addr_net_ada[addr] -= ada_val
        addr_outgoing_ada[addr] += ada_val
        addr_gross_ada[addr] += ada_val
        addr_tx_map[addr].add(tx_hash)

    for addr, ada_val in output_addrs.items():
        if addr not in addr_type:
            addr_type[addr] = classify_address(addr).value
        addr_net_ada[addr] += ada_val
        addr_incoming_ada[addr] += ada_val
        addr_gross_ada[addr] += ada_val
        addr_tx_map[addr].add(tx_hash)


def _compute_direction(source: str, target: str, target_address: str) -> str:
    """Determine direction of interaction relative to target_address."""
    if source == target_address and target == target_address:
        return "both"
    if source == target_address:
        return "outgoing"  # target sent to `target`
    if target == target_address:
        return "incoming"  # target received from `source`
    # Both are third-party addresses — direction is from source→target
    return "unknown"


def _format_errors(errors: list[str]) -> Optional[str]:
    if not errors:
        return None
    text = "; ".join(errors[:5])
    if len(errors) > 5:
        text += f" (+{len(errors) - 5} more)"
    return text
