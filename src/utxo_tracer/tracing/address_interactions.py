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

from ..cache import save_transaction
from ..cex.registry import identify_cex
from ..models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    UTxONode,
)
from ..providers.base import Provider
from ..utils import address_stake_key, classify_address

logger = logging.getLogger(__name__)

# stake-key cache so same-wallet (change-address) checks stay cheap
_stake_cache: dict[str, Optional[str]] = {}


def _same_wallet(a: str, b: str) -> bool:
    """True if two addresses share a non-None stake key (same wallet).

    Used to recognise a user's OWN change addresses so they are not counted as
    third-party counterparties (which inflates the forward interaction set).
    """
    if a == b:
        return True
    if a not in _stake_cache:
        _stake_cache[a] = address_stake_key(a)
    if b not in _stake_cache:
        _stake_cache[b] = address_stake_key(b)
    sa, sb = _stake_cache[a], _stake_cache[b]
    return sa is not None and sa == sb

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
    status_callback: Optional[Callable[[str], None]] = None,
    tx_cache_get: Optional[Callable[[str], Optional[dict]]] = None,
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
    tx_cache_get:
        ``tx_hash -> tx_data | None`` lookup into the global tx cache. When it
        returns a populated dict (``input_utxos`` or ``outputs`` present) the
        tx is processed from cache with NO provider call. Only genuinely
        missing (or previously-failed, hence uncached) txs hit the provider.
        This is what makes a smaller-depth (or repeat) trace serve entirely
        from cache, and a larger-depth trace reuse everything already seen.
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
    discovered_utxos: dict[str, UTxONode] = {}
    total_tx_processed = 0
    per_addr_tx_count: dict[str, int] = defaultdict(int)
    # ADA accumulators must count each tx exactly once. The same tx can be
    # re-encountered when expanding a counterparty at a deeper BFS level; only
    # the first encounter contributes to net/gross/in/out ADA (edges are still
    # recorded every time, then deduped later by (source,target)).
    ada_counted_txs: set[str] = set()
    truncated = False  # set if the _MAX_ADDRESSES safety cap was hit

    # ── BFS loop ─────────────────────────────────────────────────────────
    while queue:
        current_addr, current_depth = queue.popleft()

        if current_depth >= max_depth:
            continue

        # ── Step 1: Fetch ALL tx hashes for this address ────────────────
        # This call can paginate for a while; tell the UI so the progress
        # line shows life instead of appearing to hang (UTXO-trace parity).
        if status_callback:
            status_callback(
                f"fetching tx list: {current_addr[:14]}…"
                + (f" (depth {current_depth})" if max_depth > 1 else "")
            )
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
            errors.append(
                f"{current_addr[:16]}…: Failed tx fetch: {type(e).__name__}: {e}"
            )
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
                current_addr[:20],
                total_fetched,
                _MAX_TX_LIMIT,
            )

        # Remove already-processed tx hashes (cache extension)
        if skip_tx_hashes and current_addr in skip_tx_hashes:
            tx_hashes = [h for h in tx_hashes if h not in skip_tx_hashes[current_addr]]

        effective_count = len(tx_hashes)
        if effective_count == 0:
            continue

        total_tx_processed += effective_count
        per_addr_tx_count[current_addr] = effective_count

        # Hard cap on total addresses
        if len(visited_addresses) >= _MAX_ADDRESSES and current_depth > 0:
            logger.warning(
                "Reached %d address limit — stopping expansion", _MAX_ADDRESSES
            )
            truncated = True
            break

        # ── Step 2: Fetch tx details — try batch, fall back to concurrent ─
        sem = asyncio.Semaphore(10)
        level_done = [0]  # txs completed at THIS address level (monotonic)

        def _take_ada(tx_hash: str) -> bool:
            """First-encounter guard so a tx's ADA is counted exactly once."""
            if tx_hash in ada_counted_txs:
                return False
            ada_counted_txs.add(tx_hash)
            return True

        async def _examine_tx(tx_hash: str) -> None:
            """Fetch one tx and record edges + ADA flow (per-tx fallback path)."""
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

                # Cross-cache: save tx data so any trace type can reuse it
                save_transaction(tx_hash, tx_data)
                _collect_utxos(tx_data, discovered_utxos)

                input_addrs = _extract_input_addrs(tx_data)
                output_addrs = _extract_output_addrs(tx_data)
                _record_tx_edges(
                    tx_hash,
                    current_addr,
                    current_depth,
                    input_addrs,
                    output_addrs,
                    all_edges,
                    addr_tx_map,
                    addr_net_ada,
                    addr_gross_ada,
                    addr_incoming_ada,
                    addr_outgoing_ada,
                    addr_type,
                    addr_depth,
                    queue,
                    visited_addresses,
                    max_depth,
                    count_ada=_take_ada(tx_hash),
                )

                if step_callback:
                    step_callback(current_addr, tx_hash, None, current_depth)

        async def _run_concurrent_single() -> None:
            """Per-tx concurrent fetch — streams progress as each completes."""
            tasks = [asyncio.create_task(_examine_tx(h)) for h in tx_hashes]
            completed = 0
            pending: set[asyncio.Task] = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                completed += len(done)
                await asyncio.sleep(0)  # yield so Rich progress can render
                if progress_callback:
                    await progress_callback(completed, effective_count)

        async def _process_chunk(chunk: list[str]) -> None:
            """Fetch one batch chunk and process its txs.

            Raises ``NotImplementedError`` (no batch support) straight through
            so the caller can fall back to per-tx fetching.
            """
            async with sem:
                batch_results = await asyncio.wait_for(
                    provider.get_transactions_utxos(chunk),
                    timeout=timeout_per_fetch * (len(chunk) + 2),
                )
            for tx_hash, tx_data in zip(chunk, batch_results):
                save_transaction(tx_hash, tx_data)
                _process_tx_data_static(
                    tx_hash,
                    current_addr,
                    current_depth,
                    tx_data,
                    all_edges,
                    addr_tx_map,
                    addr_net_ada,
                    addr_gross_ada,
                    addr_incoming_ada,
                    addr_outgoing_ada,
                    addr_type,
                    addr_depth,
                    queue,
                    visited_addresses,
                    max_depth,
                    errors,
                    discovered_utxos,
                    count_ada=_take_ada(tx_hash),
                )
                has_data = bool(tx_data.get("input_utxos") or tx_data.get("outputs"))
                tx_err = None if has_data else "empty result"
                if not has_data:
                    errors.append(f"{current_addr[:16]}…: {tx_hash[:16]}…: {tx_err}")
                if step_callback:
                    step_callback(current_addr, tx_hash, tx_err, current_depth)
                # Increment + report with NO await between them so the count
                # stays strictly monotonic even across concurrent chunks.
                level_done[0] += 1
                if progress_callback:
                    await progress_callback(level_done[0], effective_count)
                await asyncio.sleep(0)  # let Rich render between txs

        # ── Cache-serve pass ────────────────────────────────────────────
        # Split this level's txs into cached (already in the global tx store)
        # and uncached. Cached txs are processed with NO provider call, so a
        # smaller-depth / repeat trace replays instantly from cache and a
        # larger-depth trace only spends provider quota on genuinely new txs.
        cached_pairs: list[tuple[str, dict]] = []
        uncached: list[str] = list(tx_hashes)
        if tx_cache_get is not None:
            uncached = []
            for h in tx_hashes:
                try:
                    cd = tx_cache_get(h)
                except Exception:
                    cd = None
                # Require input_utxos: address edges are input→output, so a
                # cached tx missing its input side would silently under-count
                # edges. Such txs are re-fetched from the provider instead.
                if cd and cd.get("input_utxos") and cd.get("outputs"):
                    cached_pairs.append((h, cd))
                else:
                    uncached.append(h)

        if cached_pairs and status_callback:
            status_callback(
                f"cache: {len(cached_pairs)} tx"
                + (f", {len(uncached)} via provider" if uncached else "")
            )
        for h, cd in cached_pairs:
            _process_tx_data_static(
                h,
                current_addr,
                current_depth,
                cd,
                all_edges,
                addr_tx_map,
                addr_net_ada,
                addr_gross_ada,
                addr_incoming_ada,
                addr_outgoing_ada,
                addr_type,
                addr_depth,
                queue,
                visited_addresses,
                max_depth,
                errors,
                discovered_utxos,
                count_ada=_take_ada(h),
            )
            if step_callback:
                step_callback(current_addr, h, None, current_depth)
            level_done[0] += 1
            if progress_callback:
                await progress_callback(level_done[0], effective_count)
            await asyncio.sleep(0)  # stream cached progress like the UTXO trace

        # Small chunks with several in flight → the progress bar advances
        # continuously instead of jumping after each blocking batch call (the
        # "fetch everything then just log" symptom). Behaves like the
        # streaming UTXO trace. Only uncached txs reach the provider.
        chunk_size = 4
        chunks = [
            uncached[i : i + chunk_size]
            for i in range(0, len(uncached), chunk_size)
        ]
        if uncached and status_callback:
            status_callback(
                f"provider: {len(uncached)} tx"
                + (f" (depth {current_depth})" if max_depth > 1 else "")
            )

        # Route by REAL batch capability — never by a blocking probe.
        #
        # Providers WITHOUT a true batch endpoint (blockfrost, maestro, kupmios)
        # take the per-tx concurrent path: each tx is its own task and reports
        # progress the instant it completes (FIRST_COMPLETED), so the bar streams
        # smoothly exactly like the UTXO trace — no "fetch everything then log"
        # stall, and no dead-air probe of the first chunk.
        #
        # Providers WITH a real batch endpoint (Koios /tx_info) fan all chunks
        # out concurrently; a chunk that errors falls back to per-tx fetching.
        if getattr(provider, "supports_batch_tx_fetch", False) and chunks:
            results = await asyncio.gather(
                *[_process_chunk(c) for c in chunks],
                return_exceptions=True,
            )
            for c, r in zip(chunks, results):
                if isinstance(r, Exception):
                    if not isinstance(r, NotImplementedError):
                        logger.debug("Chunk failed: %s — per-tx fallback", r)
                    for h in c:
                        await _examine_tx(h)
        elif uncached:
            await _run_concurrent_single()

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
        detail_tx_count = len(addr_tx_map.get(addr, set()))
        addr_level_count = per_addr_tx_count.get(addr, 0)
        nodes.append(
            AddressInteractionNode(
                address=addr,
                address_type=addr_type.get(addr, classify_address(addr).value),
                total_ada=round(addr_gross_ada.get(addr, 0.0), 6),
                net_ada=round(addr_net_ada.get(addr, 0.0), 6),
                total_incoming_ada=round(addr_incoming_ada.get(addr, 0.0), 6),
                total_outgoing_ada=round(addr_outgoing_ada.get(addr, 0.0), 6),
                tx_count=detail_tx_count or addr_level_count,
                is_cex=cex is not None,
                cex_name=cex.name if cex else "",
                is_target=(addr == target_address),
                depth=addr_depth.get(addr, 0),
            )
        )

    # Aggregate edges by (source, target) pair
    edge_map: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for in_addr, out_addr, tx_hash, depth in all_edges:
        edge_map[(in_addr, out_addr)].append((tx_hash, depth))

    edges: list[AddressInteractionEdge] = []
    for (source, target), tx_list in edge_map.items():
        direction = _compute_direction(source, target, target_address, addr_depth)
        min_depth = min(d for _, d in tx_list)
        edges.append(
            AddressInteractionEdge(
                source=source,
                target=target,
                tx_hashes=[h for h, _ in tx_list],
                interaction_count=len(tx_list),
                direction_relative_to_target=direction,
                source_depth=min_depth,
            )
        )

    if truncated:
        errors.insert(
            0,
            f"Reached {_MAX_ADDRESSES}-address safety cap — graph truncated; "
            f"narrow the trace (lower --max-depth or set --tx-limit)",
        )

    if not edges and total_tx_processed > 0 and errors:
        logger.warning(
            "Provider returned empty data for ALL %d transactions of %s — "
            "check API key permissions (need /txs/{hash}/utxos access)",
            total_tx_processed,
            target_address[:20],
        )

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

            save_utxos_to_store(list(discovered_utxos.values()))
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


def _collect_utxos(tx_data: dict, discovered: dict[str, UTxONode]) -> None:
    """Cross-cache: collect all UTXOs from tx data (deduped by node_id)."""
    for iutxo in tx_data.get("input_utxos", {}).values():
        if iutxo is not None and iutxo.address:
            discovered[iutxo.id] = iutxo
    for out in tx_data.get("outputs", []):
        if out is not None and out.address:
            discovered[out.id] = out


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
    count_ada: bool = True,
) -> None:
    """Record directed edges + ADA flow for one tx, queuing new addresses.

    ``count_ada`` is False when this tx's ADA was already accumulated on an
    earlier BFS encounter — edges are still recorded (deduped downstream) but
    net/gross/in/out ADA are not double-counted.
    """

    # Directed edges: input → output, keep only pairs where current_addr is one side
    for in_addr in input_addrs:
        for out_addr in output_addrs:
            if in_addr == out_addr:
                continue
            if in_addr == current_addr or out_addr == current_addr:
                all_edges.append((in_addr, out_addr, tx_hash, current_depth))

                # Track counterparty for BFS expansion. Skip the user's OWN
                # change addresses (same wallet / stake key): they are not
                # third-party counterparties, so traversing into them would
                # inflate the graph with self-owned nodes.
                other = out_addr if in_addr == current_addr else in_addr
                if (
                    other not in visited_addresses
                    and current_depth + 1 < max_depth
                    and not _same_wallet(other, current_addr)
                ):
                    visited_addresses.add(other)
                    queue.append((other, current_depth + 1))

    # Record tx membership for EVERY address in this tx on every encounter
    # (set dedups by tx_hash). Independent of count_ada so tx_count is not
    # under-counted when an address is first seen on a later BFS level.
    for addr in set(input_addrs) | set(output_addrs):
        addr_tx_map[addr].add(tx_hash)

    # Update per-address ADA data (once per tx — see count_ada)
    if count_ada:
        _update_addr_data(
            input_addrs,
            output_addrs,
            tx_hash,
            addr_tx_map,
            addr_net_ada,
            addr_gross_ada,
            addr_incoming_ada,
            addr_outgoing_ada,
            addr_type,
            addr_depth,
            current_depth,
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
            addr_depth[addr] = current_depth + 1
        addr_net_ada[addr] -= ada_val
        addr_outgoing_ada[addr] += ada_val
        addr_gross_ada[addr] += ada_val
        addr_tx_map[addr].add(tx_hash)

    for addr, ada_val in output_addrs.items():
        if addr not in addr_type:
            addr_type[addr] = classify_address(addr).value
        if addr not in addr_depth:
            addr_depth[addr] = current_depth + 1
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
    discovered_utxos: dict[str, UTxONode],
    count_ada: bool = True,
) -> None:
    """Process pre-fetched tx_data (batch path)."""
    _collect_utxos(tx_data, discovered_utxos)

    input_addrs = _extract_input_addrs(tx_data)
    output_addrs = _extract_output_addrs(tx_data)

    _record_tx_edges(
        tx_hash,
        current_addr,
        current_depth,
        input_addrs,
        output_addrs,
        all_edges,
        addr_tx_map,
        addr_net_ada,
        addr_gross_ada,
        addr_incoming_ada,
        addr_outgoing_ada,
        addr_type,
        addr_depth,
        queue,
        visited_addresses,
        max_depth,
        count_ada=count_ada,
    )


def _compute_direction(
    source: str,
    target: str,
    target_address: str,
    addr_depth: Optional[dict[str, int]] = None,
) -> str:
    """Determine direction of an edge relative to the traced target address.

    Edges are recorded as ``source -> target`` = value flow (source spent,
    target received). Direct edges touching the target are exact. For multi-hop
    edges (neither endpoint is the target) the BFS depth tells which endpoint is
    closer to the target: value moving toward the target = ``incoming``, away =
    ``outgoing``. Equal-depth (lateral) edges remain ``unknown``.
    """
    if source == target_address and target == target_address:
        return "both"
    if source == target_address:
        return "outgoing"
    if target == target_address:
        return "incoming"
    if addr_depth:
        ds = addr_depth.get(source)
        dt = addr_depth.get(target)
        if ds is not None and dt is not None and ds != dt:
            # closer-to-target endpoint = smaller depth
            return "outgoing" if ds < dt else "incoming"
    return "unknown"


def _format_errors(errors: list[str]) -> Optional[str]:
    if not errors:
        return None
    text = "; ".join(errors[:5])
    if len(errors) > 5:
        text += f" (+{len(errors) - 5} more)"
    return text


# ── CEX-related post-filter ───────────────────────────────────────────────


def apply_cex_filter(result: AddressTraceResult) -> AddressTraceResult:
    """Reduce an AddressTraceResult to only addresses on a path from
    *target_address* to any CEX address in the registry.

    "On a path to a CEX" means: an address X is kept if there exists at
    least one CEX C in the trace graph such that the path from the target
    to C passes through X. In other words, X is an ancestor of C in the
    BFS tree rooted at the target.

    Why this semantic
    ----------------
    In a typical BFS trace, the target is the central hub: every discovered
    address shares a tx with the target (directly or via a short chain).
    A naive "BFS-reachable from any CEX" filter would therefore keep the
    ENTIRE graph as soon as a single CEX is found, which defeats the
    purpose of filtering a large graph. The "ancestors of CEX up to
    target" semantic keeps only the CEX-touching branches and drops
    unrelated ones — which is what an investigator wants when scanning
    a 500-node trace for CEX exposure.

    Algorithm
    ---------
    1. Build undirected adjacency from ``result.edges``.
    2. BFS from ``target_address`` and record each visited node's parent
       in the BFS tree. Nodes unreachable from the target are dropped
       (they shouldn't be in a valid trace, but be defensive).
    3. For each CEX found in ``result.addresses`` (using ``is_cex`` and
       a re-check via ``identify_cex()`` to honor registry updates after
       the trace ran), walk back from the CEX to the target via parents,
       marking every node on that path.
    4. Kept set = union of all marked paths ∪ {target}.
    5. Filter both ``result.addresses`` and ``result.edges`` to that set.

    Edge cases
    ----------
    - **No CEX in result** → only the target is kept; the user sees an
      empty CEX graph and a summary hint to increase ``--max-depth``.
    - **Target itself is a registered CEX** → target is the CEX seed;
      BFS parents include every address in the trace, so the entire
      graph is kept (which is the correct behavior — if the target is a
      CEX, every interactor is CEX-related).
    - **Multiple CEXs** → union of their ancestor paths.
    - **CEX not reachable from target in BFS tree** (e.g., a CEX that
      is in the result but disconnected — shouldn't happen in a normal
      BFS trace, but the filter handles it by skipping that CEX).

    Returns
    -------
    A new ``AddressTraceResult`` with filtered ``addresses``/``edges``.
    The ``total_transactions``, ``error``, ``max_depth``, and
    ``provider_name`` fields are preserved (the trace itself is not
    re-run).
    """
    target = result.target_address
    if not result.addresses:
        return result

    # 1. Undirected adjacency
    adj: dict[str, set[str]] = defaultdict(set)
    for e in result.edges:
        if e.source and e.target:
            adj[e.source].add(e.target)
            adj[e.target].add(e.source)

    # 2. BFS from target, track parents (None = root)
    parent: dict[str, Optional[str]] = {target: None}
    bfs_queue: deque[str] = deque([target])
    while bfs_queue:
        cur = bfs_queue.popleft()
        for nb in adj.get(cur, ()):
            if nb not in parent:
                parent[nb] = cur
                bfs_queue.append(nb)

    # 3. CEX seed set: prefer result.is_cex, but also re-check via
    #    identify_cex() so registry updates after the trace are honored.
    cex_in_result: set[str] = set()
    for n in result.addresses:
        if n.is_cex or identify_cex(n.address) is not None:
            cex_in_result.add(n.address)

    # 4. Build kept set: ancestors of each CEX up to target.
    #    - If no CEX in result: keep only target (give a useful empty filter).
    #    - If target itself is a CEX: keep the entire BFS tree (every
    #      descendant of the target is CEX-related by definition).
    #    - Otherwise: walk back from each non-target CEX to target via
    #      BFS parents; union all such paths.
    keep: set[str] = {target}
    non_target_cex = cex_in_result - {target}
    if non_target_cex:
        for cex_addr in non_target_cex:
            cur: Optional[str] = cex_addr
            # Walk back via BFS parents. A CEX that is unreachable from
            # target in the BFS tree (i.e., not in parent dict) is
            # silently skipped — it shouldn't be in a valid trace.
            while cur is not None and cur in parent:
                keep.add(cur)
                cur = parent[cur]
    elif target in cex_in_result:
        # Target is a registered CEX → every reachable node is CEX-related
        keep = set(parent.keys())

    # 5. Filter addresses
    kept_addresses = [n for n in result.addresses if n.address in keep]

    # 6. Filter edges (both endpoints must be in keep)
    kept_edges = [e for e in result.edges if e.source in keep and e.target in keep]

    return AddressTraceResult(
        target_address=target,
        addresses=kept_addresses,
        edges=kept_edges,
        total_transactions=result.total_transactions,
        error=result.error,
        provider_name=result.provider_name,
        max_depth=result.max_depth,
    )
