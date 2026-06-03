"""Edge case tests: empty address, circular trace, max-depth 0, self-tx, pipeline.

All tests use mocked providers only — no real network access.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from utxo_tracer.models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    Asset,
    OutRef,
    UTxONode,
)
from utxo_tracer.tracing.address_interactions import trace_address_interactions

# Import helpers from the address interactions test suite
from tests.test_address_interactions import (
    _make_mock_provider,
    _tx_data,
    _utxo,
    _TARGET_ADDR,
    _VALID_ADDR,
)


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case 1: Empty address (0 transactions)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_address(mock_provider):
    """Address with 0 transactions → result has target only, no edges, no crash.

    The mock_provider fixture already returns [] for get_address_transactions.
    The trace should gracefully return an AddressTraceResult with just the
    target address node and zero edges.
    """
    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=_TARGET_ADDR,
        max_depth=1,
    )

    assert isinstance(result, AddressTraceResult)
    assert result.target_address == _TARGET_ADDR
    assert result.total_transactions == 0
    assert result.error is None
    assert len(result.edges) == 0, "No transactions → zero edges"

    # Should have exactly the target address node
    target_nodes = [n for n in result.addresses if n.address == _TARGET_ADDR]
    assert len(target_nodes) == 1, "Target address must appear exactly once"
    assert target_nodes[0].is_target is True
    assert target_nodes[0].depth == 0
    assert target_nodes[0].tx_count == 0
    assert target_nodes[0].total_ada == 0.0
    assert target_nodes[0].net_ada == 0.0

    # No extra stray addresses
    assert len(result.addresses) == 1, (
        f"Expected 1 address (target only), got {len(result.addresses)}"
    )


@pytest.mark.asyncio
async def test_empty_address_no_provider_calls(mock_provider):
    """Empty address: provider transaction detail functions never called."""
    mock_provider.get_address_transactions.return_value = []

    await trace_address_interactions(
        provider=mock_provider,
        target_address="addr_test_empty_again",
        max_depth=2,
    )

    # No transactions → no detail fetches at all
    mock_provider.get_transaction_utxos.assert_not_called()
    mock_provider.get_transactions_utxos.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case 2: Circular trace (A → B → A)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circular_trace_terminates(monkeypatch):
    """Circular A→B→A trace: BFS terminates via visited set, no infinite loop.

    A (target) sends to B in tx1. B sends back to A in tx2.
    When BFS reaches B at depth 1 and discovers tx2 where A is recipient,
    A is already in visited_addresses, so A is NOT re-queued.

    Result: 2 addresses (A, B), 2 edges (A→B, B→A), finite processing.
    """
    from utxo_tracer.tracing.address_interactions import trace_address_interactions
    from unittest.mock import patch

    A = "addr_test_circular_A"
    B = "addr_test_circular_B"

    # with patch("utxo_tracer.tracing.address_interactions.save_transaction"):
    mock = AsyncMock()
    mock.current_provider = "blockfrost"

    # A's transactions: tx1 (A → B)
    # B's transactions: tx2 (B → A)
    async def get_addr_txs(addr: str) -> list[str]:
        return {
            A: ["tx1_A_to_B"],
            B: ["tx2_B_to_A"],
        }.get(addr, [])

    mock.get_address_transactions = AsyncMock(side_effect=get_addr_txs)

    async def get_tx_utxos(tx_hash: str):
        if tx_hash == "tx1_A_to_B":
            return _tx_data(
                "tx1_A_to_B",
                inputs=[_utxo(OutRef(tx_hash="in_tx1", output_index=0), A, 10.0)],
                outputs=[_utxo(OutRef(tx_hash="tx1_A_to_B", output_index=0), B, 10.0)],
            )
        elif tx_hash == "tx2_B_to_A":
            return _tx_data(
                "tx2_B_to_A",
                inputs=[_utxo(OutRef(tx_hash="in_tx2", output_index=0), B, 10.0)],
                outputs=[_utxo(OutRef(tx_hash="tx2_B_to_A", output_index=0), A, 10.0)],
            )
        return {}

    mock.get_transaction_utxos = AsyncMock(side_effect=get_tx_utxos)
    # Batch path raises NotImplementedError → fallback to concurrent
    mock.get_transactions_utxos.side_effect = NotImplementedError

    result = await trace_address_interactions(
        provider=mock,
        target_address=A,
        max_depth=3,
    )

    # Verify finite result
    assert result.total_transactions > 0, "Should process at least tx1"

    # Both addresses present
    addresses = {n.address for n in result.addresses}
    assert A in addresses
    assert B in addresses

    # No infinite expansion — should NOT have addresses beyond {A, B}
    assert len(addresses) == 2, (
        f"Circular trace should only have 2 addresses (A, B), "
        f"got {len(addresses)}: {addresses}"
    )

    # Edges: A→B (from tx1) and B→A (from tx2)
    edge_pairs = {(e.source, e.target) for e in result.edges}
    assert (A, B) in edge_pairs, "Missing A→B edge from tx1"
    assert (B, A) in edge_pairs, "Missing B→A edge from tx2"
    assert len(result.edges) == 2, f"Expected 2 edges, got {len(result.edges)}"

    # Depth: A=0, B=1 (not re-queued at depth 2)
    depths = {n.address: n.depth for n in result.addresses}
    assert depths[A] == 0
    assert depths[B] == 1


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case 3: max_depth = 0
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_depth_zero(mock_provider):
    """max_depth=0: only target address returned, no transaction processing.

    The BFS loop checks ``if current_depth >= max_depth: continue``.
    For target at depth 0 with max_depth=0, the check fires BEFORE
    any transaction fetch. Result is minimal — target address only.
    """
    # Setup provider to verify it's never queried for tx details
    mock_provider.get_address_transactions.return_value = [
        "tx_abc",
    ]

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=_TARGET_ADDR,
        max_depth=0,
    )

    # Provider should NOT have been called
    mock_provider.get_address_transactions.assert_not_called()
    mock_provider.get_transaction_utxos.assert_not_called()
    mock_provider.get_transactions_utxos.assert_not_called()

    # Result structure
    assert isinstance(result, AddressTraceResult)
    assert result.target_address == _TARGET_ADDR
    assert result.max_depth == 0
    assert result.total_transactions == 0
    assert result.error is None
    assert len(result.edges) == 0

    # Only target address
    assert len(result.addresses) == 1
    node = result.addresses[0]
    assert node.address == _TARGET_ADDR
    assert node.is_target is True
    assert node.depth == 0
    assert node.tx_count == 0


@pytest.mark.asyncio
async def test_max_depth_zero_ignores_provider_data(mock_provider):
    """max_depth=0: rich provider data is completely ignored."""
    mock_provider.get_address_transactions.return_value = [
        "tx_hash_1234",
        "tx_hash_5678",
    ]

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address="addr_test_depth_zero_ignore",
        max_depth=0,
    )

    # No tx detail calls
    mock_provider.get_transaction_utxos.assert_not_called()
    mock_provider.get_transactions_utxos.assert_not_called()

    # Single address, zero transactions
    assert len(result.addresses) == 1
    assert result.total_transactions == 0


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case 4: Self-transaction (target in both inputs and outputs)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_transaction(monkeypatch):
    """Target address in both inputs and outputs → self-edge dropped, no crash.

    _record_tx_edges has guard: ``if in_addr == out_addr: continue``.
    This drops self-edges. The trace should complete without error,
    producing a result with the self-tx counted but no self-edge.
    """
    mock = AsyncMock()
    mock.current_provider = "blockfrost"

    A = "addr_test_self_tx_A"
    B = "addr_test_self_tx_B"

    # A has transaction where A is both input and output (self-tx)
    # plus a normal transaction to B
    async def get_addr_txs(addr: str) -> list[str]:
        return {
            A: ["tx_self", "tx_A_to_B"],
            B: [],
        }.get(addr, [])

    mock.get_address_transactions = AsyncMock(side_effect=get_addr_txs)

    async def get_tx_utxos(tx_hash: str):
        if tx_hash == "tx_self":
            # A is both input and output (self-spend + change)
            return _tx_data(
                "tx_self",
                inputs=[_utxo(OutRef(tx_hash="in_self", output_index=0), A, 5.0)],
                outputs=[
                    _utxo(OutRef(tx_hash="tx_self", output_index=0), A, 4.0),
                    _utxo(OutRef(tx_hash="tx_self", output_index=1), A, 1.0),
                ],
            )
        elif tx_hash == "tx_A_to_B":
            return _tx_data(
                "tx_A_to_B",
                inputs=[_utxo(OutRef(tx_hash="in_AtB", output_index=0), A, 10.0)],
                outputs=[_utxo(OutRef(tx_hash="tx_A_to_B", output_index=0), B, 10.0)],
            )
        return {}

    mock.get_transaction_utxos = AsyncMock(side_effect=get_tx_utxos)
    mock.get_transactions_utxos.side_effect = NotImplementedError

    result = await trace_address_interactions(
        provider=mock,
        target_address=A,
        max_depth=2,
    )

    assert result.error is None, f"Self-tx should not cause error: {result.error}"
    assert result.total_transactions == 2, (
        f"Expected 2 txs processed (self + A→B), got {result.total_transactions}"
    )

    # Addresses present
    addresses = {n.address for n in result.addresses}
    assert A in addresses
    assert B in addresses

    # No self-edge (A→A should be dropped)
    for edge in result.edges:
        assert not (edge.source == A and edge.target == A), (
            "Self-edge A→A should not appear"
        )

    # Normal edge A→B should exist
    edge_pairs = {(e.source, e.target) for e in result.edges}
    assert (A, B) in edge_pairs, "Normal A→B edge should exist"


@pytest.mark.asyncio
async def test_self_transaction_only(mock_provider):
    """Address only transacts with itself → no edges, no crash, just target node."""
    A = "addr_test_self_only"

    mock_provider.get_address_transactions.return_value = ["tx_self"]
    mock_provider.get_transaction_utxos.return_value = _tx_data(
        "tx_self",
        inputs=[_utxo(OutRef(tx_hash="in_self", output_index=0), A, 5.0)],
        outputs=[
            _utxo(OutRef(tx_hash="tx_self", output_index=0), A, 3.0),
            _utxo(OutRef(tx_hash="tx_self", output_index=1), A, 2.0),
        ],
    )

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=A,
        max_depth=1,
    )

    assert result.error is None
    assert result.total_transactions == 1
    assert len(result.addresses) == 1  # only A
    assert len(result.edges) == 0  # self-edge dropped


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case 5: End-to-end address trace pipeline
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_address_trace_pipeline(monkeypatch):
    """End-to-end address trace with realistic multi-hop data.

    Target A sends to B and C (depth 1). B sends to D (depth 2).
    Verifies AddressTraceResult shape: correct nodes, edges, depths, ADA flow.
    """
    mock = AsyncMock()
    mock.current_provider = "blockfrost"

    A = "addr_test_pipeline_A"
    B = "addr_test_pipeline_B"
    C = "addr_test_pipeline_C"
    D = "addr_test_pipeline_D"

    # A → B (10 ADA), A → C (5 ADA)
    # B → D (3 ADA), B → change back to B (7 ADA)
    async def get_addr_txs(addr: str) -> list[str]:
        return {
            A: ["tx_A_to_BC"],
            B: ["tx_B_to_D"],
            C: [],
            D: [],
        }.get(addr, [])

    mock.get_address_transactions = AsyncMock(side_effect=get_addr_txs)

    async def get_tx_utxos(tx_hash: str):
        if tx_hash == "tx_A_to_BC":
            return _tx_data(
                "tx_A_to_BC",
                inputs=[_utxo(OutRef(tx_hash="in_A", output_index=0), A, 15.0)],
                outputs=[
                    _utxo(OutRef(tx_hash="tx_A_to_BC", output_index=0), B, 10.0),
                    _utxo(OutRef(tx_hash="tx_A_to_BC", output_index=1), C, 5.0),
                ],
            )
        elif tx_hash == "tx_B_to_D":
            return _tx_data(
                "tx_B_to_D",
                inputs=[_utxo(OutRef(tx_hash="in_B", output_index=0), B, 10.0)],
                outputs=[
                    _utxo(OutRef(tx_hash="tx_B_to_D", output_index=0), D, 3.0),
                    _utxo(OutRef(tx_hash="tx_B_to_D", output_index=1), B, 7.0),
                ],
            )
        return {}

    mock.get_transaction_utxos = AsyncMock(side_effect=get_tx_utxos)
    mock.get_transactions_utxos.side_effect = NotImplementedError

    result = await trace_address_interactions(
        provider=mock,
        target_address=A,
        max_depth=3,
    )

    # ── Structure checks ──────────────────────────────────────────────────
    assert isinstance(result, AddressTraceResult)
    assert result.target_address == A
    assert result.max_depth == 3
    assert result.provider_name == "blockfrost"
    assert result.error is None
    # total_transactions counts effective tx hashes per address level:
    # A has 1 tx (tx_A_to_BC), B has 1 tx (tx_B_to_D) = 2 total
    assert result.total_transactions == 2, (
        f"Expected 2 txs processed, got {result.total_transactions}"
    )

    # ── Addresses ─────────────────────────────────────────────────────────
    addresses = {n.address: n for n in result.addresses}
    assert set(addresses.keys()) == {A, B, C, D}, (
        f"Expected {A, B, C, D}, got {set(addresses.keys())}"
    )

    # Target
    assert addresses[A].is_target is True
    assert addresses[A].depth == 0
    # A sent 15 ADA total (10 to B + 5 to C)
    assert addresses[A].total_outgoing_ada == pytest.approx(15.0)
    assert addresses[A].net_ada == pytest.approx(-15.0)

    # B at depth 1
    assert addresses[B].is_target is False
    assert addresses[B].depth == 1
    # B received 10 from A (tx_A_to_BC) + 7 change from self (tx_B_to_D) = 17 incoming
    assert addresses[B].total_incoming_ada == pytest.approx(17.0)
    # B sent 10 to D+self (tx_B_to_D input) = 10 outgoing
    assert addresses[B].total_outgoing_ada == pytest.approx(10.0)
    # net = incoming - outgoing = 17 - 10 = 7
    assert addresses[B].net_ada == pytest.approx(7.0)

    # C at depth 1
    assert addresses[C].is_target is False
    assert addresses[C].depth == 1
    assert addresses[C].total_incoming_ada == pytest.approx(5.0)
    assert addresses[C].total_outgoing_ada == 0.0
    assert addresses[C].net_ada == pytest.approx(5.0)

    # D at depth 2
    assert addresses[D].is_target is False
    assert addresses[D].depth == 2
    assert addresses[D].total_incoming_ada == pytest.approx(3.0)
    assert addresses[D].total_outgoing_ada == 0.0

    # ── Edges ─────────────────────────────────────────────────────────────
    edge_pairs = {(e.source, e.target) for e in result.edges}
    assert (A, B) in edge_pairs, "Missing A→B edge"
    assert (A, C) in edge_pairs, "Missing A→C edge"
    assert (B, D) in edge_pairs, "Missing B→D edge"

    # No self-edge B→B
    for e in result.edges:
        assert e.source != e.target, "Self-edge should be dropped"

    # 3 unique (source, target) pairs
    assert len(result.edges) == 3, f"Expected 3 edges, got {len(result.edges)}"

    # ── Direction relative to target ──────────────────────────────────────
    for e in result.edges:
        if e.source == A:
            assert e.direction_relative_to_target == "outgoing"
        elif e.target == A:
            assert e.direction_relative_to_target == "incoming"
        else:
            assert e.direction_relative_to_target == "unknown"

    # ── Each AddressInteractionNode has correct type ────────────────────
    for node in result.addresses:
        assert isinstance(node, AddressInteractionNode)
        assert node.address
        assert node.address_type in ("wallet", "script", "byron", "stake", "unknown")
        assert node.depth >= 0


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case: Concurrent path fallback on NotImplementedError
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_not_implemented_falls_back_to_concurrent(mock_provider):
    """When get_transactions_utxos raises NotImplementedError, falls back to
    concurrent get_transaction_utxos (singular) path."""
    addr = "addr_test_batch_fallback"
    tx_hash = "tx_fallback_test"

    mock_provider.get_address_transactions.return_value = [tx_hash]
    mock_provider.get_transactions_utxos.side_effect = NotImplementedError
    mock_provider.get_transaction_utxos.return_value = _tx_data(
        tx_hash,
        inputs=[_utxo(OutRef(tx_hash="in_fb", output_index=0), addr, 10.0)],
        outputs=[
            _utxo(OutRef(tx_hash=tx_hash, output_index=0), "addr_counterparty", 10.0)
        ],
    )

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=addr,
        max_depth=1,
    )

    assert result.error is None
    assert result.total_transactions == 1
    # Singular get_transaction_utxos was used (batch raised NotImplementedError)
    mock_provider.get_transaction_utxos.assert_called()
    mock_provider.get_transactions_utxos.assert_called()


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case: Transaction error handling (concurrent path)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tx_error_tracking_concurrent_path(mock_provider):
    """Singular tx fetch errors are tracked in result.error."""
    addr = "addr_test_tx_error"
    tx_hashes = ["tx_good", "tx_bad"]

    mock_provider.get_address_transactions.return_value = tx_hashes
    mock_provider.get_transactions_utxos.side_effect = NotImplementedError

    call_count = [0]

    async def get_tx(tx_hash: str):
        call_count[0] += 1
        if call_count[0] == 2:  # second call = tx_bad
            raise ValueError("Simulated API error")
        return _tx_data(
            tx_hash,
            inputs=[_utxo(OutRef(tx_hash=f"in_{tx_hash}", output_index=0), addr, 5.0)],
            outputs=[_utxo(OutRef(tx_hash=tx_hash, output_index=0), "addr_ok", 5.0)],
        )

    mock_provider.get_transaction_utxos = AsyncMock(side_effect=get_tx)

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=addr,
        max_depth=1,
    )

    assert result.error is not None, "Should track error for failed tx"
    assert "ValueError" in result.error
    assert result.total_transactions == 2, "Both txs counted (even failed)"


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case: Address tx fetch failure
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_address_tx_fetch_failure(mock_provider):
    """When get_address_transactions raises, error is captured in result."""
    mock_provider.get_address_transactions.side_effect = ConnectionError("Network down")

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address="addr_test_fetch_fail",
        max_depth=1,
    )

    assert result.error is not None
    assert "ConnectionError" in result.error
    assert result.total_transactions == 0
    assert len(result.addresses) == 1  # target only
    assert len(result.edges) == 0


# ──────────────────────────────────────────────────────────────────────────────
# Edge Case: tx_limit capping
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tx_limit_enforced(mock_provider):
    """tx_limit caps the number of txs processed per address level."""
    addr = "addr_test_tx_limit"
    many_tx = [f"tx_{i:04d}" for i in range(100)]

    mock_provider.get_address_transactions.return_value = many_tx
    mock_provider.get_transactions_utxos.side_effect = NotImplementedError
    mock_provider.get_transaction_utxos.return_value = _tx_data(
        "tx_0000",
        inputs=[_utxo(OutRef(tx_hash="in_00", output_index=0), addr, 1.0)],
        outputs=[
            _utxo(OutRef(tx_hash="tx_0000", output_index=0), "addr_counterparty", 1.0)
        ],
    )

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=addr,
        max_depth=1,
        tx_limit=5,
    )

    # Only 5 txs processed (capped by tx_limit)
    assert result.total_transactions == 5, (
        f"tx_limit=5 should cap to 5, got {result.total_transactions}"
    )
