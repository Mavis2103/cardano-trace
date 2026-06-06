"""Health check tests for all providers and fallback chain."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from utxo_tracer.providers.blockfrost import BlockfrostProvider
from utxo_tracer.providers.fallback import FallbackProvider
from utxo_tracer.providers.koios import KoiosProvider
from utxo_tracer.providers.kupmios import KupmiosProvider
from utxo_tracer.providers.maestro import MaestroProvider
from utxo_tracer.providers.utxorpc import UTxORPCProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_get_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    async def _get(*args, **kwargs):
        return resp
    return _get


def _make_post_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    async def _post(*args, **kwargs):
        return resp
    return _post


# ── Blockfrost ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blockfrost_health_ok():
    """Blockfrost health check: /blocks/latest 200 -> True."""
    provider = BlockfrostProvider(api_key="key")
    provider._client = AsyncMock()
    provider._client.get.side_effect = _make_get_response(200, {})
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_blockfrost_health_fail():
    """Blockfrost health check: exception -> False."""
    provider = BlockfrostProvider(api_key="key")
    provider._client = AsyncMock()
    async def _raise(*args, **kwargs):
        raise httpx.ConnectError("down")
    provider._client.get.side_effect = _raise
    assert await provider.health_check() is False


# ── Koios ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_koios_health_ok():
    """Koios health check: POST /tip 200 -> True."""
    provider = KoiosProvider()
    provider._client = AsyncMock()
    provider._client.post.side_effect = _make_post_response(200, {})
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_koios_health_fail():
    """Koios health check: exception -> False."""
    provider = KoiosProvider()
    provider._client = AsyncMock()
    async def _raise(*args, **kwargs):
        raise httpx.ConnectError("down")
    provider._client.post.side_effect = _raise
    assert await provider.health_check() is False


# ── Maestro ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_maestro_health_ok():
    """Maestro health check: /health 200 -> True."""
    provider = MaestroProvider(api_key="key")
    provider._client = AsyncMock()
    provider._client.get.side_effect = _make_get_response(200, {})
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_maestro_health_chain_tip_fallback():
    """Maestro health: /health fails -> /chain-tip 200 -> True."""
    provider = MaestroProvider(api_key="key")
    provider._client = AsyncMock()
    call_count = [0]

    async def _multi(*args, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        if call_count[0] == 1:
            resp.status_code = 500  # /health fails
        else:
            resp.status_code = 200  # /chain-tip succeeds
        return resp

    provider._client.get.side_effect = _multi
    assert await provider.health_check() is True
    assert call_count[0] == 2


# ── Kupmios ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kupmios_health_kupo_ok():
    """Kupmios health: Kupo /health 200 -> True."""
    provider = KupmiosProvider()
    provider._kupo = AsyncMock()
    provider._ogmios = AsyncMock()
    provider._kupo.get.side_effect = _make_get_response(200, {})
    provider._ogmios.get.side_effect = _make_get_response(200, {})
    assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_kupmios_health_ogmios_down():
    """Kupmios health: Kupo OK + Ogmios fail -> still True."""
    provider = KupmiosProvider()
    provider._kupo = AsyncMock()
    provider._ogmios = AsyncMock()
    provider._kupo.get.side_effect = _make_get_response(200, {})

    async def _ogmios_raise(*args, **kwargs):
        raise httpx.ConnectError("ogmios down")
    provider._ogmios.get.side_effect = _ogmios_raise
    assert await provider.health_check() is True


# ── UTxORPC ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_utxorpc_health_ok():
    """UTxORPC health: async_read_params succeeds -> True."""
    with patch.object(UTxORPCProvider, '_get_query_client') as mock_gqc:
        mock_qc = AsyncMock()
        mock_qc.async_read_params.return_value = MagicMock()
        mock_gqc.return_value = mock_qc

        provider = UTxORPCProvider(api_key="key", base_url="utxorpc.example.com:443")
        assert await provider.health_check() is True


@pytest.mark.asyncio
async def test_utxorpc_health_fail():
    """UTxORPC health: async_read_params raises -> False."""
    with patch.object(UTxORPCProvider, '_get_query_client') as mock_gqc:
        mock_qc = AsyncMock()
        mock_qc.async_read_params.side_effect = Exception("gRPC error")
        mock_gqc.return_value = mock_qc

        provider = UTxORPCProvider(api_key="key", base_url="utxorpc.example.com:443")
        assert await provider.health_check() is False


# ── Fallback ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_health_chain():
    """Fallback health: first fails, second succeeds -> True."""
    mock1 = AsyncMock()
    mock1.health_check.return_value = False
    mock2 = AsyncMock()
    mock2.health_check.return_value = True

    fb = FallbackProvider([("bad", mock1), ("good", mock2)])
    assert await fb.health_check() is True
