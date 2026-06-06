"""CLI surface lock.

Guards the public command/option surface so refactors of ``cli.py`` (e.g.
factoring shared options into the ``connection_options`` decorator) cannot
silently drop a command or flag. If you intentionally add/rename a command or
option, update the expected sets below in the same change.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from utxo_tracer.cli import PROVIDER_CHOICES, main

# Every command / subcommand the CLI must expose, addressed by its path.
EXPECTED_COMMANDS = {
    ("trace-utxo",),
    ("trace-address",),
    ("assets",),
    ("health",),
    ("open",),
    ("config", "set"),
    ("config", "show"),
    ("config", "clear"),
    ("cache", "list"),
    ("cache", "clear"),
    ("cache", "info"),
    ("cex", "cashflow"),
    ("cex", "env"),
    ("cex", "import"),
    ("cex", "report"),
    ("cex", "template"),
    ("cex", "reconcile-all"),
    ("cex", "hacker-detect"),
    ("cex", "cache", "list"),
    ("cex", "cache", "clear"),
}

# Options the shared connection_options decorator must put on every command
# that builds a provider.
CONNECTION_OPTS = {
    "--provider",
    "--api-key",
    "--base-url",
    "--auth-type",
    "--endpoint-url",
    "--kupo-url",
    "--ogmios-url",
    "--use-proxy",
    "--proxy-url",
}
PROVIDER_COMMANDS = [("trace-utxo",), ("trace-address",), ("health",), ("assets",)]


def _resolve(path: tuple[str, ...]) -> click.Command:
    cmd: click.Command = main
    for part in path:
        assert isinstance(cmd, click.Group), f"{part!r} parent is not a group"
        cmd = cmd.commands[part]
    return cmd


def _walk(cmd: click.Command, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(cmd, click.Group):
        out: set[tuple[str, ...]] = set()
        for name, sub in cmd.commands.items():
            out |= _walk(sub, prefix + (name,))
        return out
    return {prefix}


def _option_names(cmd: click.Command) -> set[str]:
    names: set[str] = set()
    for p in cmd.params:
        if isinstance(p, click.Option):
            names.update(p.opts)
            names.update(p.secondary_opts)
    return names


def test_command_set_is_exact():
    """No command added or dropped without updating this test."""
    actual = _walk(main)
    assert actual == EXPECTED_COMMANDS, (
        f"missing={EXPECTED_COMMANDS - actual} extra={actual - EXPECTED_COMMANDS}"
    )


def test_every_command_help_works():
    """Every command's --help renders (catches broken option wiring)."""
    runner = CliRunner()
    for path in EXPECTED_COMMANDS:
        result = runner.invoke(main, list(path) + ["--help"])
        assert result.exit_code == 0, f"{' '.join(path)} --help failed:\n{result.output}"


def test_provider_commands_have_connection_options():
    """trace/trace-address/health/assets all carry the shared connection flags."""
    for path in PROVIDER_COMMANDS:
        opts = _option_names(_resolve(path))
        missing = CONNECTION_OPTS - opts
        assert not missing, f"{' '.join(path)} missing connection options: {missing}"


def test_fallback_is_only_where_expected():
    """--fallback belongs to trace/trace-address/health, NOT assets."""
    assert "--fallback" in _option_names(_resolve(("trace-utxo",)))
    assert "--fallback" in _option_names(_resolve(("trace-address",)))
    assert "--fallback" in _option_names(_resolve(("health",)))
    assert "--fallback" not in _option_names(_resolve(("assets",)))


def test_provider_choices_consistent():
    """--provider on trace exposes exactly the supported providers."""
    for p in _resolve(("trace-utxo",)).params:
        if isinstance(p, click.Option) and "--provider" in p.opts:
            assert isinstance(p.type, click.Choice)
            assert list(p.type.choices) == PROVIDER_CHOICES
            break
    else:  # pragma: no cover - guard
        raise AssertionError("trace has no --provider option")
