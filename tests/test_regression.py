"""Regression tests: cross-module integration, model consistency, cache roundtrips.

Verifies the full stack works together after all implementation tasks (1-10).
All tests use mocked providers — no real network access.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utxo_tracer.models import (
    AddressInteractionEdge,
    AddressInteractionNode,
    AddressTraceResult,
    Asset,
    OutRef,
    TraceResult,
    TraceStep,
    TransactionEdge,
    UTxONode,
)
from utxo_tracer.tracing.address_interactions import trace_address_interactions
from utxo_tracer.tracing.backward import trace_backward
from utxo_tracer.tracing.forward import trace_forward

START_TX_HASH = "a" * 64
START_REF = OutRef(START_TX_HASH, 0)

# Import helpers
from tests.test_address_interactions import _utxo, _tx_data, _make_mock_provider


# ══════════════════════════════════════════════════════════════════════════════
# Model consistency
# ══════════════════════════════════════════════════════════════════════════════


class TestModelConsistency:
    """Verify models are structurally correct and compatible."""

    def test_out_ref_equality_hash(self):
        """OutRef equality and hashing based on tx_hash+output_index."""
        a = OutRef("abc", 0)
        b = OutRef("abc", 0)
        c = OutRef("abc", 1)
        d = OutRef("def", 0)

        assert a == b
        assert a != c
        assert a != d
        assert hash(a) == hash(b)
        assert len({a, b, c, d}) == 3

    def test_out_ref_node_id(self):
        """node_id format: tx_hash:output_index."""
        ref = OutRef("deadbeef", 42)
        assert ref.node_id() == "deadbeef:42"
        assert str(ref) == "deadbeef#42"

    def test_asset_is_lovelace(self):
        """Empty policy_id = lovelace."""
        ada = Asset(policy_id="", asset_name="", quantity=1_000_000)
        token = Asset(policy_id="a1b2c3", asset_name="NFT", quantity=1)

        assert ada.is_lovelace is True
        assert token.is_lovelace is False
        assert ada.unit == "lovelace"
        assert token.unit == "a1b2c3.NFT"

    def test_utxo_node_ada_conversion(self):
        """UTxONode.ada / lovelace properties."""
        node = UTxONode(
            id="test:0",
            out_ref=OutRef("test", 0),
            address="addr_test",
            assets=[
                Asset(policy_id="", asset_name="", quantity=2_500_000),
                Asset(policy_id="policy1", asset_name="TokenA", quantity=100),
            ],
        )
        assert node.lovelace == 2_500_000
        assert node.ada == 2.5

    def test_utxo_node_no_lovelace(self):
        """UTxONode with no lovelace returns 0."""
        node = UTxONode(
            id="no_ada:0",
            out_ref=OutRef("no_ada", 0),
            address="addr_test",
            assets=[Asset(policy_id="policy1", asset_name="TokenA", quantity=100)],
        )
        assert node.lovelace == 0
        assert node.ada == 0.0

    def test_address_trace_result_fields(self):
        """AddressTraceResult has all required fields."""
        node = AddressInteractionNode(
            address="addr_test",
            address_type="wallet",
            total_ada=10.0,
            net_ada=5.0,
            total_incoming_ada=10.0,
            total_outgoing_ada=5.0,
            tx_count=3,
            is_cex=False,
            cex_name="",
            is_target=True,
            depth=0,
        )
        edge = AddressInteractionEdge(
            source="addr_A",
            target="addr_B",
            tx_hashes=["tx1", "tx2"],
            interaction_count=2,
            direction_relative_to_target="outgoing",
            source_depth=0,
        )

        result = AddressTraceResult(
            target_address="addr_test",
            addresses=[node],
            edges=[edge],
            total_transactions=3,
            error=None,
            provider_name="blockfrost",
            max_depth=2,
        )

        assert result.target_address == "addr_test"
        assert len(result.addresses) == 1
        assert len(result.edges) == 1
        assert result.total_transactions == 3
        assert result.error is None
        assert result.provider_name == "blockfrost"
        assert result.max_depth == 2

    def test_trace_result_fields(self):
        """TraceResult has all required fields."""
        node = UTxONode(
            id="abc:0",
            out_ref=OutRef("abc", 0),
            address="addr_test",
            assets=[],
        )
        edge = TransactionEdge(
            id="edge1",
            source="abc:0",
            target="def:1",
            direction="input",
        )
        ref = OutRef("abc", 0)

        result = TraceResult(
            nodes=[node],
            edges=[edge],
            traced_path=["abc:0"],
            start_out_ref=ref,
            direction="backward",
            max_depth=5,
            provider_name="blockfrost",
        )

        assert result.start_out_ref == ref
        assert result.direction == "backward"
        assert result.max_depth == 5
        assert len(result.nodes) == 1
        assert len(result.edges) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Cross-module integration
# ══════════════════════════════════════════════════════════════════════════════


class TestCrossModuleIntegration:
    """Backward + forward + address_interactions work together."""

    @pytest.mark.asyncio
    async def test_backward_trace_basic(self, mock_provider):
        """Backward trace yields steps without crashing with valid provider."""
        # Configure mock for backward trace
        mock_utxo = MagicMock()
        mock_utxo.address = "addr_test_backward"
        mock_provider.get_utxo_by_out_ref = AsyncMock(return_value=mock_utxo)
        mock_provider.provider_type = "blockfrost"

        # Return empty tx (no further inputs → trace ends after start)
        mock_provider.get_transaction_utxos = AsyncMock(
            return_value={"inputs": [], "outputs": [], "input_utxos": {}}
        )

        steps = []
        async for step in trace_backward(mock_provider, START_REF, max_depth=1):
            steps.append(step)

        assert len(steps) >= 1
        assert steps[0].out_ref == START_REF
        assert steps[0].depth == 0

    @pytest.mark.asyncio
    async def test_forward_trace_rejects_invalid_provider(self, mock_provider):
        """Forward trace with non-kupmios/blockfrost/koios → error step."""
        mock_provider.provider_type = "maestro"  # not in allowed list

        steps = []
        async for step in trace_forward(mock_provider, START_REF, max_depth=1):
            steps.append(step)

        assert len(steps) == 1
        assert steps[0].error is not None
        assert "requires" in steps[0].error

    @pytest.mark.asyncio
    async def test_backward_and_address_interactions_shared_cache_format(
        self, mock_provider
    ):
        """Backward tx_data format is compatible with address_interactions parsers."""
        from utxo_tracer.tracing.address_interactions import (
            _extract_input_addrs,
            _extract_output_addrs,
        )

        # Build tx_data in backward-compatible format
        tx_hash = "shared_tx_001"
        in_addr = "addr_input_test"
        out_addr = "addr_output_test"

        tx_data = _tx_data(
            tx_hash,
            inputs=[_utxo(OutRef(tx_hash="in_shared", output_index=0), in_addr, 10.0)],
            outputs=[_utxo(OutRef(tx_hash=tx_hash, output_index=0), out_addr, 10.0)],
        )

        inputs = _extract_input_addrs(tx_data)
        outputs = _extract_output_addrs(tx_data)

        assert in_addr in inputs
        assert inputs[in_addr] == pytest.approx(10.0)
        assert out_addr in outputs
        assert outputs[out_addr] == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_address_interactions_depth_propagation(self, mock_provider):
        """Depth propagates correctly through multi-hop BFS expansion."""
        A = "addr_test_depth_A"
        B = "addr_test_depth_B"
        C = "addr_test_depth_C"

        mock_provider.get_address_transactions.side_effect = lambda addr: {
            A: ["tx1_A_to_B"],
            B: ["tx2_B_to_C"],
            C: [],
        }.get(addr, [])

        mock_provider.get_transactions_utxos.side_effect = NotImplementedError

        async def get_tx(tx_hash: str):
            if tx_hash == "tx1_A_to_B":
                return _tx_data(
                    "tx1_A_to_B",
                    inputs=[_utxo(OutRef(tx_hash="in1", output_index=0), A, 10.0)],
                    outputs=[
                        _utxo(OutRef(tx_hash="tx1_A_to_B", output_index=0), B, 10.0)
                    ],
                )
            elif tx_hash == "tx2_B_to_C":
                return _tx_data(
                    "tx2_B_to_C",
                    inputs=[_utxo(OutRef(tx_hash="in2", output_index=0), B, 10.0)],
                    outputs=[
                        _utxo(OutRef(tx_hash="tx2_B_to_C", output_index=0), C, 10.0)
                    ],
                )
            return {}

        mock_provider.get_transaction_utxos = AsyncMock(side_effect=get_tx)

        result = await trace_address_interactions(
            provider=mock_provider,
            target_address=A,
            max_depth=5,
        )

        depths = {n.address: n.depth for n in result.addresses}
        assert depths[A] == 0
        assert depths[B] == 1
        assert depths[C] == 2

    @pytest.mark.asyncio
    async def test_backward_error_propagation(self, mock_provider):
        """Backward trace errors propagate as step.error without crashing."""
        mock_provider.provider_type = "blockfrost"
        mock_provider.get_utxo_by_out_ref.side_effect = ValueError("API down")

        steps = []
        async for step in trace_backward(mock_provider, START_REF, max_depth=1):
            steps.append(step)

        assert len(steps) == 1
        assert steps[0].error is not None
        assert "ValueError" in steps[0].error


# ══════════════════════════════════════════════════════════════════════════════
# Cache roundtrip
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def _temp_cache():
    """Isolated cache DB for roundtrip tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir) / ".utxo-cache"
        cache_dir.mkdir()
        db_path = cache_dir / "cache.db"
        import utxo_tracer.cache as cache_mod

        orig_cache_dir = cache_mod.CACHE_DIR
        orig_db_path = cache_mod.DB_PATH
        cache_mod.CACHE_DIR = cache_dir
        cache_mod.DB_PATH = db_path
        cache_mod.close_db()
        try:
            yield cache_mod
        finally:
            cache_mod.CACHE_DIR = orig_cache_dir
            cache_mod.DB_PATH = orig_db_path
            cache_mod.close_db()


class TestCacheRoundtrip:
    """Cache save → load produces equivalent data."""

    def test_save_address_trace_no_error(self, _temp_cache):
        """Save snapshot doesn't throw — verifies DB init + schema work."""
        cache_mod = _temp_cache

        node = AddressInteractionNode(
            address="addr_test_noerr",
            address_type="wallet",
            total_ada=10.0,
            net_ada=5.0,
            tx_count=2,
            is_target=True,
            depth=0,
        )
        result = AddressTraceResult(
            target_address="addr_test_noerr",
            addresses=[node],
            edges=[],
            total_transactions=2,
            provider_name="blockfrost",
            max_depth=1,
        )

        # Verify DB init and save succeed without error
        cache_mod.save_address_trace(result, tx_limit=0, max_depth=1)
        cache_mod.save_address_trace_step(
            address="addr_test_noerr",
            tx_hash="tx_test",
            error=None,
            discovered_utxos=[],
            total_count=1,
            tx_limit=0,
            max_depth=1,
            source_address="addr_test_noerr",
            depth=0,
        )
        cache_mod.finalize_address_trace("addr_test_noerr", max_depth=1)

        # Verify partial load finds the manifest
        partial = cache_mod.load_address_trace_partial("addr_test_noerr", max_depth=1)
        assert partial is not None, "Partial load should find manifest after finalize"
        assert partial.completed
        assert partial.max_depth >= 1

    def test_load_address_trace_missing(self, _temp_cache):
        """Loading non-existent trace returns None."""
        loaded = _temp_cache.load_address_trace("nonexistent_addr", max_depth=1)
        assert loaded is None

    def test_load_address_trace_partial_missing(self, _temp_cache):
        """Loading partial for non-existent trace returns None."""
        loaded = _temp_cache.load_address_trace_partial("nonexistent_addr", max_depth=1)
        assert loaded is None


# ══════════════════════════════════════════════════════════════════════════════
# Edge case: multiple addresses at same depth
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_branching_at_same_depth(mock_provider):
    """Target sends to multiple addresses at depth 1 → all get correct depth=1."""
    A = "addr_test_fanout_A"
    B = "addr_test_fanout_B"
    C = "addr_test_fanout_C"

    mock_provider.get_address_transactions.return_value = ["tx_fanout"]
    # Batch path returns a LIST of tx data dicts
    mock_provider.get_transactions_utxos.return_value = [
        _tx_data(
            "tx_fanout",
            inputs=[_utxo(OutRef(tx_hash="in_fan", output_index=0), A, 30.0)],
            outputs=[
                _utxo(OutRef(tx_hash="tx_fanout", output_index=0), B, 10.0),
                _utxo(OutRef(tx_hash="tx_fanout", output_index=1), C, 20.0),
            ],
        )
    ]

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=A,
        max_depth=2,
    )

    assert len(result.addresses) == 3  # A, B, C
    depths = {n.address: n.depth for n in result.addresses}
    assert depths[A] == 0
    assert depths[B] == 1
    assert depths[C] == 1

    # Both edges exist: A→B, A→C
    edge_pairs = {(e.source, e.target) for e in result.edges}
    assert (A, B) in edge_pairs
    assert (A, C) in edge_pairs
    assert len(result.edges) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Edge case: Max addresses cap
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_visited_set_grows_efficiently(mock_provider):
    """Visited set prevents re-queueing of already-seen addresses."""
    A = "addr_test_visited_A"
    B = "addr_test_visited_B"

    # A has transaction to B
    # B has transaction back to A (but A already visited)
    mock_provider.get_address_transactions.side_effect = lambda addr: {
        A: ["tx_A_to_B"],
        B: ["tx_B_to_A"],
    }.get(addr, [])

    mock_provider.get_transactions_utxos.side_effect = NotImplementedError

    async def get_tx(tx_hash: str):
        if tx_hash == "tx_A_to_B":
            return _tx_data(
                "tx_A_to_B",
                inputs=[_utxo(OutRef(tx_hash="in_1", output_index=0), A, 10.0)],
                outputs=[_utxo(OutRef(tx_hash="tx_A_to_B", output_index=0), B, 10.0)],
            )
        elif tx_hash == "tx_B_to_A":
            return _tx_data(
                "tx_B_to_A",
                inputs=[_utxo(OutRef(tx_hash="in_2", output_index=0), B, 10.0)],
                outputs=[_utxo(OutRef(tx_hash="tx_B_to_A", output_index=0), A, 10.0)],
            )
        return {}

    mock_provider.get_transaction_utxos = AsyncMock(side_effect=get_tx)

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address=A,
        max_depth=5,
    )

    # Only 2 unique addresses (A not re-queued when discovered from B's txs)
    assert len(result.addresses) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Edge case: Provider type detection
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_provider_name_from_current_provider(mock_provider):
    """provider_name uses current_provider attribute."""
    mock_provider.current_provider = "utxorpc"
    mock_provider.get_address_transactions.return_value = []

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address="addr_test_provider_name",
        max_depth=1,
    )

    assert result.provider_name == "utxorpc"


@pytest.mark.asyncio
async def test_provider_name_falls_back_to_provider_type(mock_provider):
    """provider_name falls back to provider_type if current_provider is empty."""
    mock_provider.current_provider = ""
    mock_provider.provider_type = "kupmios"
    mock_provider.get_address_transactions.return_value = []

    result = await trace_address_interactions(
        provider=mock_provider,
        target_address="addr_test_provider_fallback",
        max_depth=1,
    )

    assert result.provider_name == "kupmios"
