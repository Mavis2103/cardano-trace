from __future__ import annotations

from unittest.mock import AsyncMock

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
