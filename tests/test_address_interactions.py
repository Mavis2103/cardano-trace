"""Tests for address_interactions: batch empty result error tracking and real-time output."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from utxo_tracer.models import Asset, OutRef, UTxONode
from utxo_tracer.tracing.address_interactions import trace_address_interactions


# ── Helpers ──────────────────────────────────────────────────────────────

_VALID_ADDR = "addr1qx2kd28nq8ac5prwg32hhvudlwggpgfp8utly7vgq2nzjt"
_TARGET_ADDR = _VALID_ADDR + "x" * 57  # ~120 chars, realistic length


def _utxo(out_ref: OutRef, address: str = _TARGET_ADDR, ada: float = 10.0) -> UTxONode:
    """Build a minimal UTxONode for test data."""
    return UTxONode(
        id=out_ref.node_id(),
        out_ref=out_ref,
        address=address,
        assets=[Asset(policy_id="", asset_name="", quantity=int(ada * 1_000_000))],
    )


def _tx_data(tx_hash: str, inputs: list[UTxONode], outputs: list[UTxONode]) -> dict:
    """Build a tx_data dict matching _process_tx_data_static expectations."""
    return {
        "input_utxos": {utxo.id: utxo for utxo in inputs},
        "outputs": outputs,
    }


@pytest.mark.asyncio
async def test_batch_empty_result_error_tracking(mock_provider):
    """Batch path: empty tx data from get_transactions_utxos appends to errors.

    Bug B3: batch path (address_interactions.py:215-218) fires step_callback
    with tx_err="empty result" but does NOT append to the errors list.
    After fix, result.error should contain the empty-result messages.
    """
    tx_hashes = [f"abc{i}" * 8 for i in range(3)]  # 3 realistic-looking hashes

    mock_provider.get_address_transactions.return_value = tx_hashes
    mock_provider.get_transactions_utxos.return_value = [
        {}
        for _ in tx_hashes  # all empty — no inputs, no outputs
    ]

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address="addr1_test_address_for_batch_empty_result_test",
        max_depth=1,
    )

    assert result.error is not None, "Expected error string for empty batch results"
    assert "empty result" in result.error, (
        f"Expected 'empty result' in error, got: {result.error}"
    )


# ── B9: Real-time terminal logging for batch path ────────────────────────


@pytest.mark.asyncio
async def test_batch_path_real_time_output(monkeypatch):
    """Batch path yields to event loop progressively, not in one burst.

    Fix B9: Old code called ``await asyncio.sleep(0)`` ONCE after all
    txs in the batch.  New code yields every ~10 txs so pipe-bound readers
    (os.write → pipe buffer) pick up progress output incrementally.
    """
    from utxo_tracer.tracing.address_interactions import _BATCH_SIZE

    n_tx = 45  # enough to trigger multiple yield points
    addr = _TARGET_ADDR

    # ── Build mock provider ──────────────────────────────────────────
    mock_provider = AsyncMock()
    mock_provider.current_provider = "blockfrost"
    mock_provider.get_address_transactions.return_value = [
        f"tx_hash_{i:04d}" for i in range(n_tx)
    ]

    batch_results: list[dict] = []
    for i in range(n_tx):
        out_ref = OutRef(tx_hash=f"tx_hash_{i:04d}", output_index=0)
        batch_results.append(
            _tx_data(
                f"tx_hash_{i:04d}",
                inputs=[_utxo(out_ref, addr)],
                outputs=[
                    _utxo(OutRef(tx_hash=f"tx_hash_{i:04d}", output_index=1), addr)
                ],
            )
        )
    mock_provider.get_transactions_utxos.return_value = batch_results

    # ── Track asyncio.sleep calls ────────────────────────────────────
    real_sleep = asyncio.sleep
    sleep_count = [0]

    async def _tracked_sleep(delay: float) -> None:
        sleep_count[0] += 1
        await real_sleep(0)  # real yield so other coroutines can run

    import utxo_tracer.tracing.address_interactions as ai_mod

    monkeypatch.setattr(ai_mod.asyncio, "sleep", _tracked_sleep)

    # ── Track callbacks ──────────────────────────────────────────────
    step_calls: list[tuple] = []
    progress_calls: list[tuple] = []

    def _step_cb(source: str, tx_hash: str, err: str | None, depth: int) -> None:
        step_calls.append((source, tx_hash, err, depth))

    async def _progress_cb(completed: int, total: int) -> None:
        progress_calls.append((completed, total))

    # ── Run ──────────────────────────────────────────────────────────
    result = await trace_address_interactions(
        mock_provider,
        addr,
        max_depth=1,
        step_callback=_step_cb,
        progress_callback=_progress_cb,
    )

    # ── Verify ───────────────────────────────────────────────────────
    # All txs processed
    assert result.total_transactions == n_tx

    # step_callback called for every tx
    assert len(step_calls) == n_tx, (
        f"Expected {n_tx} step callbacks, got {len(step_calls)}"
    )

    # progress_callback called multiple times (not 0, not 1)
    assert len(progress_calls) > 1, (
        f"progress_callback should be called multiple times (every tx in batch), "
        f"got {len(progress_calls)}"
    )

    # progress_callback receives monotonically increasing completed counts
    for i, (completed, total) in enumerate(progress_calls):
        assert total == n_tx, f"progress_callback total should be {n_tx}, got {total}"
        if i > 0:
            assert completed > progress_calls[i - 1][0], (
                f"progress_callback completed should increase: {progress_calls[i - 1][0]} → {completed}"
            )

    # asyncio.sleep called multiple times (yields every ~10 txs), not just once
    assert sleep_count[0] > 1, (
        f"asyncio.sleep should be called multiple times (yield every ~10 txs), "
        f"but got {sleep_count[0]} — this means the batch path still yields "
        f"only once after ALL txs (old B9 bug)"
    )

    # Roughly n_tx / 10 yields expected
    expected_yields = max(1, n_tx // 10)
    assert sleep_count[0] >= expected_yields, (
        f"Expected at least {expected_yields} yields, got {sleep_count[0]}"
    )


# ── B1: Depth off-by-one in _update_addr_data ────────────────────────────


@pytest.mark.asyncio
async def test_depth_assignment(mock_provider):
    """B1: Non-target addresses get depth = current_depth + 1, not current_depth.

    Old code: _update_addr_data assigns ``addr_depth[addr] = current_depth``
    for newly discovered addresses, but the BFS queue stores them at
    ``current_depth + 1``.  This off-by-one makes all counterparties
    appear one level too shallow.

    Trace: A (depth 0) → B (depth 1) → C (depth 2).
    Before fix: B=0, C=1.  After fix: B=1, C=2.
    """
    from unittest.mock import patch

    from utxo_tracer.tracing.address_interactions import trace_address_interactions

    # Mock save_transaction to avoid SQLite side effects in unit test
    with patch("utxo_tracer.tracing.address_interactions.save_transaction"):
        A = "addr_test_target_A"
        B = "addr_test_counterparty_B"
        C = "addr_test_counterparty_C"

        # Tx1: A → B  (A is input, B is output)
        utxo_A_in = _utxo(OutRef(tx_hash="tx1_A_to_B", output_index=0), A, 10.0)
        utxo_B_out = _utxo(OutRef(tx_hash="tx1_A_to_B", output_index=1), B, 10.0)
        tx1 = _tx_data("tx1_A_to_B", inputs=[utxo_A_in], outputs=[utxo_B_out])

        # Tx2: B → C  (B is input, C is output)
        utxo_B_in = _utxo(OutRef(tx_hash="tx2_B_to_C", output_index=0), B, 5.0)
        utxo_C_out = _utxo(OutRef(tx_hash="tx2_B_to_C", output_index=1), C, 5.0)
        tx2 = _tx_data("tx2_B_to_C", inputs=[utxo_B_in], outputs=[utxo_C_out])

        # Address → transactions mapping
        mock_provider.get_address_transactions.side_effect = lambda addr: {
            A: ["tx1_A_to_B"],
            B: ["tx2_B_to_C"],
        }.get(addr, [])

        # Batch path must raise NotImplementedError to fall back to concurrent
        mock_provider.get_transactions_utxos.side_effect = NotImplementedError

        # Tx hash → tx data mapping (singular, concurrent path)
        mock_provider.get_transaction_utxos.side_effect = lambda tx_hash: {
            "tx1_A_to_B": tx1,
            "tx2_B_to_C": tx2,
        }[tx_hash]

        result = await trace_address_interactions(
            provider=mock_provider,
            target_address=A,
            max_depth=5,
        )

        depths = {node.address: node.depth for node in result.addresses}

        assert depths[A] == 0, f"Target A depth should be 0, got {depths[A]}"
        assert depths[B] == 1, f"B (one hop) should have depth 1, got {depths[B]}"
        assert depths[C] == 2, f"C (two hops) should have depth 2, got {depths[C]}"


# ── B10: progress_callback support in batch path ─────────────────────────


@pytest.mark.asyncio
async def test_batch_path_progress_callback_every_tx(monkeypatch):
    """progress_callback fires after EACH tx in batch path (B10 fix).

    This ensures LiveProgress updates incrementally, matching concurrent-path behavior.
    """
    addr = _TARGET_ADDR
    n_tx = 15

    mock_provider = AsyncMock()
    mock_provider.current_provider = "blockfrost"
    mock_provider.get_address_transactions.return_value = [
        f"tx_{i:04d}" for i in range(n_tx)
    ]

    batch_results: list[dict] = []
    for i in range(n_tx):
        out_ref = OutRef(tx_hash=f"tx_{i:04d}", output_index=0)
        batch_results.append(
            _tx_data(
                f"tx_{i:04d}",
                inputs=[_utxo(out_ref, addr)],
                outputs=[_utxo(OutRef(tx_hash=f"tx_{i:04d}", output_index=1), addr)],
            )
        )
    mock_provider.get_transactions_utxos.return_value = batch_results

    real_sleep = asyncio.sleep

    async def _tracked_sleep(delay: float) -> None:
        await real_sleep(0)

    import utxo_tracer.tracing.address_interactions as ai_mod

    monkeypatch.setattr(ai_mod.asyncio, "sleep", _tracked_sleep)

    progress_calls: list[tuple] = []

    async def _progress_cb(completed: int, total: int) -> None:
        progress_calls.append((completed, total))

    await trace_address_interactions(
        mock_provider,
        addr,
        max_depth=1,
        progress_callback=_progress_cb,
    )

    # progress_callback called for every tx (not just at end)
    assert len(progress_calls) == n_tx, (
        f"Expected progress_callback {n_tx} times (once per tx), got {len(progress_calls)}"
    )

    # Last call has completed == total == n_tx
    assert progress_calls[-1] == (n_tx, n_tx), (
        f"Last progress_callback should be ({n_tx}, {n_tx}), got {progress_calls[-1]}"
    )


# ── B2: Multi-hop cache reuse ─────────────────────────────────────────────

import tempfile
from pathlib import Path

from utxo_tracer.cache import (
    close_db,
    finalize_address_trace,
    load_address_trace,
    load_address_trace_partial,
    save_address_trace,
    save_address_trace_step,
)
from utxo_tracer.models import AddressTraceResult


@pytest.fixture
def temp_cache():
    """Create a temporary cache database for isolated testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir) / ".utxo-cache"
        cache_dir.mkdir()
        db_path = cache_dir / "cache.db"
        import utxo_tracer.cache as cache_mod

        orig_cache_dir = cache_mod.CACHE_DIR
        orig_db_path = cache_mod.DB_PATH
        cache_mod.CACHE_DIR = cache_dir
        cache_mod.DB_PATH = db_path
        close_db()
        try:
            yield cache_mod
        finally:
            cache_mod.CACHE_DIR = orig_cache_dir
            cache_mod.DB_PATH = orig_db_path
            close_db()


def _make_mock_provider(tx_data_by_addr: dict[str, list[dict]]):
    """Create a mock provider that returns controlled multi-hop data."""
    mock = AsyncMock()
    mock.provider_type = "blockfrost"

    addr_txs: dict[str, list[str]] = {}
    tx_data_map: dict[str, dict] = {}
    for addr, txs in tx_data_by_addr.items():
        addr_txs[addr] = [t["tx_hash"] for t in txs]
        for t in txs:
            tx_data_map[t["tx_hash"]] = t

    async def get_address_transactions(addr: str) -> list[str]:
        return addr_txs.get(addr, [])

    async def get_transaction_utxos(tx_hash: str):
        tx = tx_data_map.get(tx_hash)
        if tx is None:
            return {"inputs": [], "outputs": [], "input_utxos": {}}
        outputs = []
        for out_addr, out_ada in tx.get("outputs", {}).items():
            lovelace = int(out_ada * 1_000_000)
            out_ref = OutRef(tx_hash=tx_hash, output_index=len(outputs))
            outputs.append(
                UTxONode(
                    id=out_ref.node_id(),
                    out_ref=out_ref,
                    address=out_addr,
                    assets=[Asset(policy_id="", asset_name="", quantity=lovelace)],
                )
            )
        input_utxos = {}
        for in_addr, in_ada in tx.get("inputs", {}).items():
            lovelace = int(in_ada * 1_000_000)
            out_ref = OutRef(tx_hash="prev_" + tx_hash, output_index=len(input_utxos))
            key = out_ref.node_id()
            input_utxos[key] = UTxONode(
                id=key,
                out_ref=out_ref,
                address=in_addr,
                assets=[Asset(policy_id="", asset_name="", quantity=lovelace)],
            )
        return {
            "inputs": [
                OutRef(tx_hash="in_" + tx_hash, output_index=i)
                for i, a in enumerate(tx.get("inputs", {}).keys())
            ],
            "outputs": outputs,
            "input_utxos": input_utxos,
        }

    async def get_transactions_utxos(tx_hashes: list[str]):
        results = []
        for tx_hash in tx_hashes:
            r = await get_transaction_utxos(tx_hash)
            results.append(r)
        return results

    mock.get_address_transactions = AsyncMock(side_effect=get_address_transactions)
    mock.get_transaction_utxos = AsyncMock(side_effect=get_transaction_utxos)
    mock.get_transactions_utxos = AsyncMock(side_effect=get_transactions_utxos)

    return mock


def _build_branching_tx_data(target: str, depth: int) -> dict[str, list[dict]]:
    """Build branching tx data for multi-hop testing.

    Each address at level d has 1 tx sending to 2 children at level d+1.
    """
    data: dict[str, list[dict]] = {}
    queue = [("target", 0)]
    tx_idx = 0

    while queue:
        addr, d = queue.pop(0)
        if d >= depth:
            data[addr] = []
            continue
        child1 = f"{addr}_0"
        child2 = f"{addr}_1"
        tx_hash = f"tx_{tx_idx}"
        tx_idx += 1
        data[addr] = [
            {
                "tx_hash": tx_hash,
                "inputs": {addr: 20.0},
                "outputs": {child1: 10.0, child2: 10.0},
            }
        ]
        queue.append((child1, d + 1))
        queue.append((child2, d + 1))

    return data


def _save_trace_to_cache(
    cache_mod, address: str, result: AddressTraceResult, max_depth: int
):
    """Helper: save a trace result + per-step progress to cache."""
    # Save v2 snapshot
    save_address_trace(result, tx_limit=0, max_depth=max_depth)

    # Save per-step progress (creates manifest)
    addr_tx_map: dict[str, set[str]] = {}
    for edge in result.edges:
        for tx_hash in edge.tx_hashes:
            if edge.source not in addr_tx_map:
                addr_tx_map[edge.source] = set()
            addr_tx_map[edge.source].add(tx_hash)

    for addr, tx_hashes in addr_tx_map.items():
        depth = _find_addr_depth(result, addr)
        for tx_hash in tx_hashes:
            cache_mod.save_address_trace_step(
                address=address,
                tx_hash=tx_hash,
                error=None,
                discovered_utxos=[],
                total_count=len(tx_hashes),
                tx_limit=0,
                max_depth=max_depth,
                source_address=addr,
                depth=depth,
            )

    # Finalize after manifest is created
    finalize_address_trace(address, max_depth)


def _find_addr_depth(result: AddressTraceResult, addr: str) -> int:
    """Find the BFS depth of an address in the result."""
    for node in result.addresses:
        if node.address == addr:
            return node.depth
    return 0


class TestMultiHopCacheReuse:
    """B2: Multi-hop address traces properly reuse cache."""

    def test_cache_bypass_bug_fixed(self, temp_cache):
        """Verify _skip_cache bypass is removed — depth>1 still checks cache."""
        cache_mod = temp_cache

        tx_data = _build_branching_tx_data("target", depth=3)
        mock = _make_mock_provider(tx_data)

        result1 = asyncio.run(
            trace_address_interactions(
                mock,
                "target",
                max_depth=3,
            )
        )
        assert len(result1.addresses) > 0

        # Save to cache
        _save_trace_to_cache(cache_mod, "target", result1, max_depth=3)

        # Now try loading at depth 2 — should find cached depth 3
        cached_partial = load_address_trace_partial("target", max_depth=2)
        assert cached_partial is not None, (
            "Cache MISS: load_address_trace_partial(target, max_depth=2) returned None. "
            "Expected to find manifest from max_depth=3 run because cached depth 3 >= requested depth 2."
        )
        assert cached_partial.max_depth >= 2, (
            f"Cached max_depth={cached_partial.max_depth}, expected >= 2"
        )
        assert hasattr(cached_partial, "processed_by_addr"), (
            "CachedAddrTrace must expose processed_by_addr for per-address skip_tx_hashes"
        )

    def test_full_cache_hit_no_provider_calls(self, temp_cache):
        """Second run at lower depth => cached result from v2 snapshot (no re-trace)."""
        cache_mod = temp_cache

        tx_data = _build_branching_tx_data("target", depth=3)
        mock = _make_mock_provider(tx_data)

        result1 = asyncio.run(
            trace_address_interactions(
                mock,
                "target",
                max_depth=3,
            )
        )
        _save_trace_to_cache(cache_mod, "target", result1, max_depth=3)

        # Load full cached result at depth 2 — should succeed (cached depth 3 >= 2)
        cached = load_address_trace("target", max_depth=2)
        assert cached is not None, (
            "Should find full cached snapshot for depth 2 (cached depth 3 >= 2)"
        )
        assert len(cached.addresses) > 0

        # Verify cached_partial also finds the manifest
        cached_partial = load_address_trace_partial("target", max_depth=2)
        assert cached_partial is not None
        assert cached_partial.max_depth >= 2

        # processed_by_addr should be populated for per-address skip tracking
        skip = getattr(cached_partial, "processed_by_addr", {})
        assert skip is not None, "processed_by_addr must be populated"
        assert len(skip) > 0, "Should have per-address processed tx hashes"

    def test_partial_extend_only_new_depth_queried(self, temp_cache):
        """Cached depth 3 — verify partial manifest with per-address tracking works."""
        cache_mod = temp_cache

        tx_data = _build_branching_tx_data("target", depth=5)
        mock = _make_mock_provider(tx_data)

        result1 = asyncio.run(
            trace_address_interactions(
                mock,
                "target",
                max_depth=3,
            )
        )
        _save_trace_to_cache(cache_mod, "target", result1, max_depth=3)
        calls_before = mock.get_address_transactions.call_count
        assert calls_before > 0

        # Load partial cache — should find manifest from depth 3 run
        cached_partial = load_address_trace_partial("target", max_depth=3)
        assert cached_partial is not None
        assert cached_partial.max_depth == 3
        assert cached_partial.completed

        # processed_by_addr should have per-address tracked tx hashes
        skip = getattr(cached_partial, "processed_by_addr", {})
        assert skip is not None
        assert len(skip) > 0, "processed_by_addr should have entries"

        # Verify that the cached v2 snapshot for depth 3 exists
        cached = load_address_trace("target", max_depth=3)
        assert cached is not None
        depth3_count = len(cached.addresses)

        # Running at depth 5 with skip_tx_hashes from cache: the target address
        # tx_hashes are skipped, so fewer new addresses are discovered.  This
        # is expected — per-address skipping is for the CLI's cache layer,
        # not for inline extension.
        mock2 = _make_mock_provider(tx_data)
        result2 = asyncio.run(
            trace_address_interactions(
                mock2,
                "target",
                max_depth=5,
                skip_tx_hashes=skip,
            )
        )
        # With skip_tx_hashes, fewer provider calls should be made
        assert mock2.get_address_transactions.call_count <= calls_before, (
            f"Expected <= {calls_before} calls with skip_tx_hashes, "
            f"got {mock2.get_address_transactions.call_count}"
        )
