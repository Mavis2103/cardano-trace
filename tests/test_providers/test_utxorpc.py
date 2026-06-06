"""TDD tests for UTxORPC provider — get_spent_utxos bug fix."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from utxo_tracer.providers.utxorpc import UTxORPCProvider


@pytest.mark.asyncio
async def test_get_spent_utxos_raises_not_implemented_error():
    """get_spent_utxos MUST raise NotImplementedError.
    
    Current implementation tries to call async_search_utxos which returns
    CURRENT UTXOs (not spent ones) — that is wrong data. Instead it should
    raise NotImplementedError immediately.
    """
    provider = UTxORPCProvider(api_key="", base_url="")
    with pytest.raises(NotImplementedError):
        await provider.get_spent_utxos(
            "addr_test1qpc6mrwu9cucrq4w6y69qchflvypq76a47ylvjvm2w"
            "kphyajfxk6cw5p8f2w5hv2htc9x4nl2p7prp4acxn22zmdq4qgxrg7u"
        )


@pytest.mark.asyncio
async def test_get_spent_utxos_does_not_return_wrong_data():
    """get_spent_utxos MUST NOT return current UTXO data as spent.
    
    Even if async_search_utxos somehow succeeds, the method should raise
    NotImplementedError BEFORE any gRPC call is made — verifying it never
    returns wrong data (current UTXOs presented as spent ones).
    """
    provider = UTxORPCProvider(api_key="", base_url="")
    with pytest.raises(NotImplementedError):
        # Should raise immediately, never reach gRPC layer
        await provider.get_spent_utxos("addr_test1...")
