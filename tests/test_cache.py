"""Tests for --no-cache flag: prevent cache writes."""

from unittest.mock import patch, MagicMock, AsyncMock
from click.testing import CliRunner


def _make_async_provider():
    mp = AsyncMock()
    mp.current_provider = ""
    mp.provider_type = "mock"
    mp.__aenter__.return_value = mp
    mp.get_address_transactions.return_value = [
        "aabbccdd11223344aabbccdd11223344aabbccdd11223344aabbccdd11223344",
    ]
    mp.get_transactions_utxos.return_value = [{}]
    return mp


def _patch_all(save_funcs):
    """Start patchers for given list of cache save function paths.
    Returns (patcher_objects, mock_objects_by_name).
    """
    patchers = [patch(p) for p in save_funcs]
    patchers.append(patch("utxo_tracer.cli.load_config", return_value={}))
    patchers.append(
        patch("utxo_tracer.cache._store_to_models", return_value=({}, {}, {}))
    )
    mocks = [p.start() for p in patchers]
    return patchers, mocks[:-2]


def _stop_all(patcher_objects):
    for p in patcher_objects:
        p.stop()


def _mock_cli_deps():
    import utxo_tracer.cli as cli_mod

    cli_mod.console = MagicMock()
    cli_mod.err_console = MagicMock()
    cli_mod.LiveProgress = MagicMock()


def test_no_cache_no_write():
    """trace-address --no-cache: save functions NOT called."""
    patchers = [
        "utxo_tracer.cache.save_address_trace_step",
        "utxo_tracer.cache.save_address_trace",
        "utxo_tracer.cache.finalize_address_trace",
        "utxo_tracer.cache.save_transaction",
        "utxo_tracer.cache.save_utxos_to_store",
    ]
    started, mocks = _patch_all(patchers)
    mock_ss, mock_sa, mock_fa = mocks[0], mocks[1], mocks[2]

    try:
        _mock_cli_deps()
        from utxo_tracer.cli import trace_address_cmd

        runner = CliRunner()

        with patch("utxo_tracer.cli._build_providers") as mb:
            mb.return_value = _make_async_provider()
            runner.invoke(
                trace_address_cmd,
                [
                    "addr1_test",
                    "--no-cache",
                    "--no-dash",
                    "--max-depth",
                    "1",
                ],
            )

        mock_ss.assert_not_called()
        mock_sa.assert_not_called()
        mock_fa.assert_not_called()
    finally:
        _stop_all(started)


def test_no_cache_utxo_trace():
    """utxo-tracer trace --no-cache: save functions NOT called."""
    patchers = [
        "utxo_tracer.cache.save_trace",
        "utxo_tracer.cache.save_trace_step",
        "utxo_tracer.cache.finalize_trace",
        "utxo_tracer.cache.save_transaction",
        "utxo_tracer.cache.save_utxos_to_store",
    ]
    started, mocks = _patch_all(patchers)
    mock_st, mock_sts, mock_ft = mocks[0], mocks[1], mocks[2]

    try:
        _mock_cli_deps()
        from utxo_tracer.cli import trace_cmd

        runner = CliRunner()

        with patch("utxo_tracer.cli._build_providers") as mb:
            mp = AsyncMock()
            mp.current_provider = ""
            mp.provider_type = "mock"
            mp.__aenter__.return_value = mp
            mb.return_value = mp

            runner.invoke(
                trace_cmd,
                [
                    "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890#0",
                    "--no-cache",
                    "--no-dash",
                    "--max-depth",
                    "1",
                ],
            )

        mock_st.assert_not_called()
        mock_sts.assert_not_called()
        mock_ft.assert_not_called()
    finally:
        _stop_all(started)
