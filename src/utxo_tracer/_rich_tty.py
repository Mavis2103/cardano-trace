"""Rich progress bar — always animated in-place updates.

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


# ── Helpers (kept for reference) ──────────────────────────────────────────────


def _strip_rich_markup(text: str) -> str:
    """Remove [color] and [/color] tags so plain-text lines are readable."""
    import re

    return re.sub(
        r"\[/?(?:bold|dim|italic|underline|strike|blink|reverse|cyan|magenta|blue|green|yellow|red|white|black|bright_|dim_)\b[^\]]*\]",
        "",
        text,
    )


def _unbuffered_write(fd: int, data: bytes) -> None:
    """Write bytes, flush Python's TextIOWrapper, then fsync fd — triple guarantee."""
    try:
        import os

        os.write(fd, data)
        sys.stdout.flush()
        try:
            os.fsync(fd)
        except (OSError, AttributeError):
            pass
    except OSError:
        pass


# ── LiveProgress (always animated in-place) ───────────────────────────────────


class LiveProgress:
    """Progress bar — always Rich Progress with animated bar.

    Always uses ``Console(force_terminal=True)`` for in-place animation,
    regardless of whether stdout is a TTY or pipe.
    """

    def __init__(self, *columns, console: Optional[Console] = None, **kwargs):
        self._is_tty: bool = True
        self._columns = columns
        self._console = console
        self._kwargs = kwargs
        self._rich_progress: Optional[Progress] = None

    # ── Duck-typed Progress interface ────────────────────────────────────────

    def add_task(self, description: str, total: Optional[float] = None) -> Any:
        if self._rich_progress is None:
            self._rich_progress = Progress(
                *self._columns,
                console=self._console or Console(force_terminal=True),
                **self._kwargs,
            )
        return self._rich_progress.add_task(description, total=total)

    def update(
        self,
        task_id: Any,
        *,
        advance: float = 0,
        description: Optional[str] = None,
        **kwargs,
    ) -> None:
        if self._rich_progress is not None:
            self._rich_progress.update(
                task_id, advance=advance, description=description, **kwargs
            )

    def remove_task(self, task_id: Any) -> None:
        if self._rich_progress is not None:
            self._rich_progress.remove_task(task_id)

    def refresh(self) -> None:
        """Force immediate render (bypass 100ms Rich timer)."""
        if self._rich_progress is not None:
            live = getattr(self._rich_progress, "live", None)
            if live is not None:
                try:
                    live.refresh()
                except Exception:
                    pass

    # ── Context manager ─────────────────────────────────────────────────────

    def __enter__(self) -> "LiveProgress":
        if self._rich_progress is None:
            self._rich_progress = Progress(
                *self._columns,
                console=self._console or Console(force_terminal=True),
                **self._kwargs,
            )
        self._rich_progress.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._rich_progress is not None:
            try:
                self._rich_progress.__exit__(*args)
            except Exception:
                pass
