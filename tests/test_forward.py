"""Tests for the UTXO-precise forward trace engine."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from utxo_tracer.models import Asset, OutRef, UTxONode
from utxo_tracer.tracing.forward import trace_forward


def _utxo(tx: str, idx: int, address: str, ada: int = 1_000_000) -> UTxONode:
    return UTxONode(
        id=f"{tx}:{idx}",
        out_ref=OutRef(tx, idx),
        address=address,
        assets=[Asset(policy_id="", asset_name="", quantity=ada)],
    )


@pytest.mark.asyncio
async def test_cached_spend_map_avoids_provider(mock_provider):
    """cached_spend_map prevents the get_address_spend_map provider call."""
    address = "addr_test1_cached_address"
    start = OutRef("txhash1", 0)
    mock_provider.provider_type = "kupmios"

    mock_provider.get_utxo_by_out_ref = AsyncMock(
        return_value=_utxo("txhash1", 0, address)
    )
    mock_provider.get_address_spend_map = AsyncMock(return_value={})
    mock_provider.get_transaction_utxos = AsyncMock(
        return_value={
            "inputs": [start],
            "outputs": [_utxo("txspend", 0, "addr_recipient")],
        }
    )

    # the start UTXO was spent by tx "txspend"
    cached_spend_map = {"txhash1:0": "txspend"}

    steps = []
    async for step in trace_forward(
        mock_provider, start, max_depth=1, cached_spend_map=cached_spend_map
    ):
        steps.append(step)

    # spend-map lookup served from cache, not the provider
    mock_provider.get_address_spend_map.assert_not_called()
    assert steps[0].out_ref == start
    assert steps[0].depth == 0
    # followed the spending tx's output forward
    followed = {s.out_ref.node_id() for s in steps}
    assert "txspend:0" in followed


@pytest.mark.asyncio
async def test_unspent_utxo_terminates(mock_provider):
    """A UTXO absent from the spend map is a terminal leaf (no forward edges)."""
    address = "addr_unspent"
    start = OutRef("txhash1", 0)
    mock_provider.provider_type = "kupmios"

    mock_provider.get_utxo_by_out_ref = AsyncMock(
        return_value=_utxo("txhash1", 0, address)
    )
    mock_provider.get_address_spend_map = AsyncMock(return_value={})  # nothing spent
    mock_provider.get_transaction_utxos = AsyncMock()

    steps = [s async for s in trace_forward(mock_provider, start, max_depth=3)]

    assert len(steps) == 1
    assert steps[0].out_ref == start
    # never followed any spending tx
    mock_provider.get_transaction_utxos.assert_not_called()


@pytest.mark.asyncio
async def test_follows_only_the_spending_tx(mock_provider):
    """Forward must follow the exact spending tx's outputs, found via spend map."""
    address = "addr_a"
    start = OutRef("txA", 0)
    mock_provider.provider_type = "kupmios"

    mock_provider.get_utxo_by_out_ref = AsyncMock(
        return_value=_utxo("txA", 0, address)
    )
    # spend map says txA:0 was consumed by txB
    mock_provider.get_address_spend_map = AsyncMock(return_value={"txA:0": "txB"})
    mock_provider.get_transaction_utxos = AsyncMock(
        return_value={
            "inputs": [start],
            "outputs": [
                _utxo("txB", 0, "addr_dest1"),
                _utxo("txB", 1, "addr_dest2"),
            ],
        }
    )

    steps = [s async for s in trace_forward(mock_provider, start, max_depth=1)]

    mock_provider.get_address_spend_map.assert_awaited_once_with(address)
    mock_provider.get_transaction_utxos.assert_awaited_once_with("txB")
    followed = {s.out_ref.node_id() for s in steps}
    assert {"txB:0", "txB:1"} <= followed


@pytest.mark.asyncio
async def test_unsupported_provider_errors(mock_provider):
    """Non-forward providers emit a single depth-0 error step."""
    mock_provider.provider_type = "maestro"
    mock_provider.supports_forward = False
    steps = [
        s async for s in trace_forward(mock_provider, OutRef("tx", 0), max_depth=2)
    ]
    assert len(steps) == 1
    assert steps[0].error is not None
