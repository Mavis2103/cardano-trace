"""Tests for forward trace engine."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from utxo_tracer.models import Asset, OutRef, UTxONode


@pytest.mark.asyncio
async def test_cached_outputs_used(mock_provider):
    """cached_outputs prevents get_spent_utxos call when address is cached."""
    address = "addr_test1_cached_address"
    start_out_ref = OutRef("txhash1", 0)

    mock_provider.provider_type = "kupmios"

    utxo = UTxONode(
        id="txhash1:0",
        out_ref=start_out_ref,
        address=address,
        assets=[Asset(policy_id="", asset_name="", quantity=1_000_000)],
    )
    mock_provider.get_utxo_by_out_ref = AsyncMock(return_value=utxo)

    mock_provider.get_transaction_utxos = AsyncMock(
        return_value={
            "inputs": [OutRef("txhash0", 0)],
            "outputs": [
                UTxONode(
                    id="txhash2:1",
                    out_ref=OutRef("txhash2", 1),
                    address="addr_target",
                    assets=[],
                ),
            ],
        }
    )

    cached_outputs = {address: ["txhash2:0"]}

    steps = []
    async for step in trace_forward(
        mock_provider, start_out_ref, max_depth=2, cached_outputs=cached_outputs
    ):
        steps.append(step)

    # get_spent_utxos should NOT have been called — cached data used instead
    mock_provider.get_spent_utxos.assert_not_called()

    # Must have at least the start step
    assert len(steps) >= 1
    assert steps[0].out_ref == start_out_ref
    assert steps[0].depth == 0
    assert steps[0].utxo is not None
    assert steps[0].utxo.address == address


from utxo_tracer.tracing.forward import trace_forward  # noqa: E402
