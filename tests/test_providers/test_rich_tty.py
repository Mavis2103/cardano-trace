"""Unit tests for _rich_tty LiveProgress (always in-place animated)."""
from __future__ import annotations

from utxo_tracer._rich_tty import LiveProgress


# ── _is_tty invariant ────────────────────────────────────────────────────────

def test_is_tty_always_true():
    """_is_tty MUST be True regardless of sys.stdout.isatty()."""
    lp = LiveProgress()
    assert lp._is_tty is True


def test_is_tty_true_even_when_piped(monkeypatch):
    """_is_tty remains True even if isatty() would return False."""
    import sys

    # Simulate pipe mode by making isatty return False
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    lp = LiveProgress()
    assert lp._is_tty is True
    # Verify the outer isatty() really returns False
    assert sys.stdout.isatty() is False


# ── No pipe-mode remnants ────────────────────────────────────────────────────

def test_no_pipe_mode_branch():
    """LiveProgress has NO _task_counters attribute (removed pipe mode)."""
    lp = LiveProgress()
    assert not hasattr(lp, "_task_counters")


def test_update_does_not_increment_counter():
    """update() does NOT touch any counter dict (pipe mode removed)."""
    lp = LiveProgress()
    # add_task creates Rich progress (not a counter)
    task_id = lp.add_task("test", total=10)
    # update should not raise — no _task_counters to fail on
    lp.update(task_id, advance=1, description="updated")
    # Should still have no _task_counters
    assert not hasattr(lp, "_task_counters")


# ── Exit cleanup (no-op when never entered) ──────────────────────────────────

def test_exit_does_not_raise():
    """__exit__ does not raise even when Rich progress wasn't entered."""
    lp = LiveProgress()
    # Calling __exit__ without __enter__ should be safe
    lp.__exit__(None, None, None)
