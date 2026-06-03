"""Tests for backward trace engine."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from utxo_tracer.models import OutRef
from utxo_tracer.tracing.backward import trace_backward

START_TX_HASH = "a" * 64
START_REF = OutRef(START_TX_HASH, 0)


@pytest.mark.asyncio
async def test_backward_error_outref_valid(mock_provider):
    """Error steps must have valid OutRef (output_index >= 0)."""
    # get_utxo_by_out_ref returns a truthy value so we get past the first fetch
    mock_provider.get_utxo_by_out_ref = AsyncMock(return_value=object())
    # get_transaction_utxos raises an error -> triggers error yield
    mock_provider.get_transaction_utxos.side_effect = ValueError("API error")

    steps = []
    async for step in trace_backward(mock_provider, START_REF, max_depth=1):
        steps.append(step)

    for s in steps:
        if s.error:
            assert s.out_ref.output_index >= 0, (
                f"Invalid output_index: {s.out_ref.output_index}"
            )
            assert s.out_ref.tx_hash == START_REF.tx_hash, "tx_hash changed"
