from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_provider():
    """Mock provider returning empty data by default."""
    from utxo_tracer.providers.base import Provider

    mock = AsyncMock(spec=Provider)
    mock.get_address_transactions.return_value = []
    mock.get_transaction_utxos.return_value = {}
    mock.get_transactions_utxos.return_value = []
    return mock


@pytest.fixture
def mock_get_transactions_utxos(mock_provider):
    """Configure mock provider.get_transactions_utxos."""
    return mock_provider.get_transactions_utxos


@pytest.fixture
def mock_httpx_client():
    """Return an AsyncMock for httpx.AsyncClient.

    The mock has configurable ``.get()`` and ``.post()`` return values.
    Test functions can set ``mock.get.return_value`` or
    ``mock.post.return_value`` to an async mock that returns a fake
    ``httpx.Response``.
    """
    import httpx

    client = AsyncMock(spec=httpx.AsyncClient)
    # Default: all requests return 404
    fake_404 = MagicMock()
    fake_404.status_code = 404
    fake_404.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=fake_404
    )
    fake_404.json.return_value = {}
    async def _default_get(*args, **kwargs):
        return fake_404
    async def _default_post(*args, **kwargs):
        return fake_404
    client.get.side_effect = _default_get
    client.post.side_effect = _default_post
    return client


@pytest.fixture
def mock_grpc():
    """Return an AsyncMock for gRPC query client.

    Test functions can set method return values for UTxORPC tests.
    """
    mock = AsyncMock()
    mock.async_search_utxos.return_value = AsyncMock()
    mock.async_search_utxos.return_value.__aiter__.return_value = []
    mock.async_read_params.return_value = MagicMock()
    return mock
