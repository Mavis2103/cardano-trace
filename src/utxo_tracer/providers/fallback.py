"""Fallback provider — automatically tries alternative providers on failure
with retry logic."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

import httpx
from .base import CapabilityError, Provider
from ..models import OutRef, UTxONode

if TYPE_CHECKING:
    import grpc as _grpc_module
else:
    try:
        import grpc as _grpc_module
    except ImportError:
        _grpc_module = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ── Retry config ──────────────────────────────────────────────────────
RETRY_DELAYS = [0.5, 1.0, 2.0]  # seconds
MAX_RETRIES = len(RETRY_DELAYS)

# ── transient-error predicates ────────────────────────────────────────
_TRANSIENT_HTTPX = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


def _is_transient(exc: BaseException) -> bool:
    """Return True when retrying the operation is likely to succeed."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, _TRANSIENT_HTTPX):
        return True
    if _grpc_module is not None and isinstance(exc, _grpc_module.RpcError):
        code = exc.code()  # type: ignore[union-attr]
        if code in (
            _grpc_module.StatusCode.UNAVAILABLE,
            _grpc_module.StatusCode.DEADLINE_EXCEEDED,
        ):
            return True
    return False


# ── rich console (fallback to logging if not available) ───────────────
try:
    from rich.console import Console as _RichConsole

    _err_console: Any = _RichConsole(stderr=True)
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False
    _err_console = None


def _warn(*args: object) -> None:
    """Emit warning through rich (if available) else logger."""
    if _RICH_AVAILABLE and _err_console is not None:
        _err_console.print(*args)
    else:
        logger.warning(" ".join(str(a) for a in args))


class FallbackProvider(Provider):
    """Wrapper that tries providers in order until one succeeds.

    Each method is tried on each provider with retries.  Only transient
    errors (network timeouts, connection failures, gRPC UNAVAILABLE) are
    retried; non-transient errors (ValueError, TypeError, KeyError, etc.)
    propagate immediately to avoid wasting time on unfixable failures.
    """

    provider_type = "fallback"
    _providers: list[Provider]
    _names: list[str]
    current_provider: str = ""  # name of currently-active provider

    def __init__(self, providers: list[tuple[str, Provider]]) -> None:
        self._providers = [p for _, p in providers]
        self._names = [n for n, _ in providers]
        self._any_raised_capability = False
        if self._names:
            self.current_provider = self._names[0]

    async def __aenter__(self) -> "FallbackProvider":
        return self

    async def __aexit__(self, *args) -> None:
        for prov in self._providers:
            await prov.aclose()

    async def _retry(self, coro_func, *args, **kwargs):
        """Call coroutine with backoff retries for transient errors only."""
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                return await coro_func(*args, **kwargs)
            except Exception as e:
                last_err = e
                # Non-transient errors are never retried
                if not _is_transient(e):
                    raise
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[attempt]
                    logger.warning(
                        "Retry %d/%d after %.1fs: %s",
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
        # All retries exhausted (all transient errors)
        raise last_err or RuntimeError("All retries exhausted")

    # ── helpers ─────────────────────────────────────────────────────
    async def _try_health(self, prov: Provider) -> bool:
        """Single-attempt health check with short timeout (no retries)."""
        try:
            return await asyncio.wait_for(prov.health_check(), timeout=5.0)
        except Exception:
            return False

    async def _try_get_utxo(
        self, prov: Provider, out_ref: OutRef
    ) -> Optional[UTxONode]:
        try:
            return await asyncio.wait_for(
                self._retry(prov.get_utxo_by_out_ref, out_ref), timeout=10.0
            )
        except Exception:
            return None

    async def _try_get_tx(self, prov: Provider, tx_hash: str) -> Optional[dict]:
        """Return result dict on success (even if empty), None on failure.

        Raises ``CapabilityError`` when the provider is reachable but
        lacks the capability to satisfy the request (e.g. no DumpHistory).
        """
        try:
            result = await asyncio.wait_for(
                self._retry(prov.get_transaction_utxos, tx_hash), timeout=10.0
            )
            # Return the actual result — even if both lists are empty.
            # Caller distinguishes None (error) from empty-but-valid.
            return result
        except CapabilityError:
            self._any_raised_capability = True
            raise  # propagate — caller should treat this as "try next"
        except Exception:
            return None

    # ── interface ───────────────────────────────────────────────────
    async def health_check(self) -> bool:
        for name, prov in zip(self._names, self._providers):
            self.current_provider = name
            ok = await self._try_health(prov)
            if ok:
                _warn(f"[green]✓ {name} is reachable[/green]")
                return True
        return False

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        errors: list[str] = []
        for name, prov in zip(self._names, self._providers):
            node = await self._try_get_utxo(prov, out_ref)
            if node is not None:
                self.current_provider = name
                if name != self._names[0]:
                    _warn(
                        f"[yellow]↑ Fallback: {self._names[0]} failed, "
                        f"using {name}[/yellow]"
                    )
                return node
            errors.append(f"{name}: failed")
        self.current_provider = self._names[-1]
        logger.warning("All providers failed for %s — %s", out_ref, "; ".join(errors))
        _warn(
            f"[yellow]Fallback: all providers failed for {out_ref} — {'; '.join(errors)}[/yellow]"
        )
        return None

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        self._any_raised_capability = False
        for name, prov in zip(self._names, self._providers):
            try:
                data = await self._try_get_tx(prov, tx_hash)
            except CapabilityError:
                # Provider is reachable but lacks DumpHistory — try next
                logger.info(
                    "Provider %s lacks backward capability for tx %s: %s",
                    name,
                    tx_hash[:16],
                    "DumpHistory unavailable",
                )
                continue
            if data is not None:
                # Only accept when the provider actually returned data;
                # empty both-lists means "no data for this tx" — try next.
                if data.get("outputs") or data.get("inputs"):
                    self.current_provider = name
                    if name != self._names[0]:
                        _warn(
                            f"[yellow]↑ Fallback tx {tx_hash[:16]}…: "
                            f"{self._names[0]} failed, using {name}[/yellow]"
                        )
                    return data
                continue

        # If ALL providers lack capability (e.g. no DumpHistory),
        # log a clear message instead of silent failure
        if self._any_raised_capability:
            _warn(
                "[yellow]All providers lack backward capability "
                "(DumpHistory unavailable) — returning empty inputs[/yellow]"
            )
        self.current_provider = self._names[-1]
        logger.warning("All providers failed for tx %s", tx_hash[:16])
        return {"inputs": [], "outputs": []}

    async def get_spent_utxos(self, address: str) -> list[OutRef]:
        not_implemented_count = 0
        for prov in self._providers:
            try:
                refs = await asyncio.wait_for(
                    self._retry(prov.get_spent_utxos, address), timeout=10.0
                )
                if refs:
                    return refs
            except NotImplementedError:
                not_implemented_count += 1
                continue
            except Exception:
                continue
        # If ALL providers raised NotImplementedError, propagate it
        if not_implemented_count == len(self._providers):
            raise NotImplementedError("get_spent_utxos not supported by any provider")
        # Some providers tried (and returned empty) or errored — return empty
        return []

    async def aclose(self) -> None:
        for prov in self._providers:
            try:
                await prov.aclose()
            except Exception as e:
                logger.warning("Error closing provider: %s", e)