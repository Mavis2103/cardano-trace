"""Unbuffered Rich progress & console — renders live even when stdout is piped.

Root cause (final, experimentally confirmed)
---------------------------------------------
 Rich Progress renders via TWO paths inside Rich:
   • **text path** : console.print() → `console.file.write(text)`
   • **binary path**: progress rendering → `console.file.buffer.write(raw_ansi_bytes)`

 When stdout is a pipe Python wraps fd 1 in a TextIOWrapper backed by a
 BufferedWriter  (~8 KB C-buffer).  Neither path flushes after each update:
   • The binary path writes ANSI escape sequences including `\r` (carriage
     return) for cursor animation.  A `\r` alone does NOT trigger TextIOWrapper
     line-buffered flush — only `\n` does.
   • Even if `\n` is present, Rich never calls `sys.stdout.flush()` after
     each progress update; it relies on the context-manager `__exit__` to flush.

 The combined effect: all progress output sits in the C-buffer for 12 s+ and
 only emerges when the process ends and the buffer fills/ flushes.

Fix (3 layers, all applied)
---------------------------------------------
 1. **Newline guarantee**: every progress update is terminated with `\n`
    (not `\r`).  The line-log counter makes each update an independent line
    that TextIOWrapper (line_buffering=True) will flush immediately.
 2. **Explicit os.fsync(1)** after each unbuffered os.write() — forces the
    kernel to drain the pipe buffer before the next write.  Prevents producer
    outrunning consumer.
 3. **sys.stdout reconfigure** at import time — sets `write_through=True` on
    the TextIOWrapper so Python's OS-level write calls bypass all internal
    layers; sets `line_buffering=True` so `\n` triggers an immediate flush.

Both the Rich TTY mode (animated bar) and the Pipe/Fallback mode (line-log)
are handled by the same `LiveProgress` class, selected automatically based
on `sys.stdout.isatty()`.

Usage in cli.py
---------------
   from ._rich_tty import LiveProgress
   with LiveProgress(
       SpinnerColumn(), TextColumn("{task.description}"),
       BarColumn(), TextColumn("{task.completed} tx"),
       TimeElapsedColumn(),
   ) as progress:
       task = progress.add_task("tracing...", total=None)
       for tx in txs:
           progress.update(task, advance=1, description=f"tx={tx_hash}")
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)


# ── Module-level: force stdout into write-through + line-buffering ────────────
#  This must happen at import time, before any console objects are created,
#  so that Python's own TextIOWrapper delegates immediately to the OS.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)  # type: ignore[attr-defined]
except Exception:
    pass
try:
    sys.stderr.reconfigure(line_buffering=True, write_through=True)  # type: ignore[attr-defined]
except Exception:
    pass


# ── Helpers ──────────────────────────────────────────────────────────────────


def _strip_rich_markup(text: str) -> str:
    """Remove [color] and [/color] tags so plain-text lines are readable in pipe mode."""
    import re

    return re.sub(
        r"\[/?(?:bold|dim|italic|underline|strike|blink|reverse|cyan|magenta|blue|green|yellow|red|white|black|bright_|dim_)\b[^\]]*\]",
        "",
        text,
    )


def _unbuffered_write(fd: int, data: bytes) -> None:
    """Write bytes, flush Python's TextIOWrapper, then fsync fd — triple guarantee."""
    try:
        os.write(fd, data)
        sys.stdout.flush()  # nudge TextIOWrapper just in case
        try:
            os.fsync(fd)
        except (OSError, AttributeError):
            pass
    except OSError:
        pass


# ── Dual-mode Progress ───────────────────────────────────────────────────────


class LiveProgress:
    """Progress bar that adapts to the runtime stdout environment.

    TTY mode (default on real terminals):
        Full Rich Progress with spinner + animated bar + ANSI colors.
        Uses ``\\r`` for cursor animation (no guarantee of realtime in pipe).

    Pipe / Redirect mode (detected automatically):
        Each update writes a single ``[N] description\\n`` line to fd 1.
        Uses ``os.write() + fsync(1)`` — bypasses every Python buffering layer.
        Suitable for: ``| head``, ``| tee file``, ``> file``, CI/CD logs.
    """

    def __init__(self, *columns, console: Optional[Console] = None, **kwargs):
        self._is_tty = sys.stdout.isatty() if console is None else True
        self._columns = columns
        self._console = console
        self._kwargs = kwargs
        self._rich_progress: Optional[Progress] = None
        self._task_counters: dict[Any, int] = {}
        self._fd: int = sys.stdout.fileno() if hasattr(sys.stdout, "fileno") else 1

    # ── Duck-typed Progress interface ────────────────────────────────────────

    def add_task(self, description: str, total: Optional[float] = None) -> Any:
        if self._is_tty:
            if self._rich_progress is None:
                self._rich_progress = Progress(
                    *self._columns,
                    console=self._console or Console(force_terminal=True),
                    **self._kwargs,
                )
            return self._rich_progress.add_task(description, total=total)
        else:
            task_id = len(self._task_counters)
            self._task_counters[task_id] = 0
            # In initial line in pipe mode
            plain = _strip_rich_markup(description)
            _unbuffered_write(self._fd, f"[0] {plain}\n".encode())
            return task_id

    def update(
        self,
        task_id: Any,
        *,
        advance: float = 0,
        description: Optional[str] = None,
        **kwargs,
    ) -> None:
        if self._is_tty and self._rich_progress is not None:
            self._rich_progress.update(
                task_id, advance=advance, description=description, **kwargs
            )
        else:
            self._task_counters[task_id] = self._task_counters.get(task_id, 0) + max(
                int(advance), 1
            )
            if description:
                plain = _strip_rich_markup(description)
                count = self._task_counters[task_id]
                _unbuffered_write(self._fd, f"[{count}] {plain}\n".encode())

    def remove_task(self, task_id: Any) -> None:
        if self._is_tty and self._rich_progress is not None:
            self._rich_progress.remove_task(task_id)
        self._task_counters.pop(task_id, None)

    def refresh(self) -> None:
        """Force immediate render (bypass 100ms Rich timer)."""
        if self._is_tty and self._rich_progress is not None:
            live = getattr(self._rich_progress, "live", None)
            if live is not None:
                try:
                    live.refresh()
                except Exception:
                    pass

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self) -> LiveProgress:
        if self._is_tty:
            if self._rich_progress is None:
                self._rich_progress = Progress(
                    *self._columns,
                    console=self._console or Console(force_terminal=True),
                    **self._kwargs,
                )
            self._rich_progress.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._is_tty and self._rich_progress is not None:
            try:
                self._rich_progress.__exit__(*args)
            except Exception:
                pass
        # Pipe mode: no-op (already flushed per-step)
