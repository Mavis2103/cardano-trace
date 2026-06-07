"""Kupo provider (``provider_type = "kupmios"``).

Kupo-only. Ogmios is intentionally NOT used: the only thing it was wired for —
resolving a transaction's inputs for backward tracing — has no Ogmios method
(``findTransactions`` does not exist), and Ogmios chain-sync needs a persistent
WebSocket that the HTTP client can't hold. Kupo indexes UTXOs (outputs + their
spend lifecycle), which covers forward tracing, address tracing and single-UTXO
lookups. Backward tracing is unsupported here (``supports_backward = False``);
use utxorpc / blockfrost / koios / minibf for it.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..models import Asset, OutRef, UTxONode
from ..utils import hex_to_utf8
from .base import Provider

logger = logging.getLogger(__name__)


class KupmiosProvider(Provider):
    provider_type = "kupmios"
    supports_forward = True
    supports_backward = False  # Kupo can't resolve a tx's inputs (see module docstring)

    def __init__(
        self,
        kupo_url: str = "http://localhost:1442",
        kupo_api_key: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.kupo_url = kupo_url.rstrip("/")

        kupo_headers: dict[str, str] = {"Accept": "application/json"}
        if kupo_api_key:
            kupo_headers["dmtr-api-key"] = kupo_api_key
            kupo_headers["Authorization"] = f"Bearer {kupo_api_key}"
        self._kupo = httpx.AsyncClient(
            base_url=self.kupo_url, headers=kupo_headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._kupo.aclose()

    async def health_check(self) -> bool:
        """Return True if Kupo is reachable (sufficient for all Kupo reads)."""
        try:
            r = await self._kupo.get("/health")
            return r.status_code in (200, 204)
        except Exception:
            return False

    def _parse_kupo_match(self, m: dict) -> UTxONode:
        tx_hash = m.get("transaction_id", "")
        idx = int(m.get("output_index") or 0)
        out_ref = OutRef(tx_hash=tx_hash, output_index=idx)
        value = m.get("value", {}) or {}
        coins = int(value.get("coins") or 0)
        assets: list[Asset] = [Asset(policy_id="", asset_name="", quantity=coins)]
        for unit, qty in (value.get("assets") or {}).items():
            if "." in unit:
                policy_id, asset_name_hex = unit.split(".", 1)
            else:
                policy_id, asset_name_hex = unit, ""
            asset_name = hex_to_utf8(asset_name_hex) if asset_name_hex else ""
            assets.append(
                Asset(
                    policy_id=policy_id, asset_name=asset_name, quantity=int(qty or 0)
                )
            )
        datum_hash = m.get("datum_hash")
        # Kupo /matches returns only datum_hash + datum_type, never the datum
        # content (fetch separately via /datums/{hash}). Don't pass the hash off
        # as inline_datum — callers expect actual datum bytes or None.
        return UTxONode(
            id=out_ref.node_id(),
            out_ref=out_ref,
            address=m.get("address", ""),
            assets=assets,
            datum_hash=datum_hash,
            inline_datum=None,
            script_ref=m.get("script_hash"),
        )

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        try:
            path = f"/matches/{out_ref.output_index}@{out_ref.tx_hash}"
            r = await self._kupo.get(path)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                if not data:
                    return None
                return self._parse_kupo_match(data[0])
            if isinstance(data, dict):
                return self._parse_kupo_match(data)
            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            logger.warning("get_utxo_by_out_ref HTTP error for %s: %s", out_ref, e)
            return None
        except Exception as e:
            logger.debug("get_utxo_by_out_ref failed for %s: %s", out_ref, e)
            return None

    async def _get_all_outputs_for_tx(self, tx_hash: str) -> list[UTxONode]:
        r = await self._kupo.get(f"/matches/*@{tx_hash}")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        outputs: list[UTxONode] = []
        if isinstance(data, list):
            for m in data:
                outputs.append(self._parse_kupo_match(m))
        return outputs

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        """Return the transaction's outputs. Inputs are always empty.

        Kupo indexes outputs (via ``/matches/*@{tx}``) but has no way to list
        the inputs a transaction consumed, so ``inputs`` is always ``[]`` and
        backward tracing is unsupported (``supports_backward = False``). Forward
        tracing uses only the outputs returned here.
        """
        outputs = await self._get_all_outputs_for_tx(tx_hash)
        return {"inputs": [], "outputs": outputs}

    async def get_address_transactions(self, address: str) -> list[str]:
        """Return all transaction hashes involving this address via Kupo."""
        try:
            r = await self._kupo.get(f"/matches/{address}")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            tx_hashes: set[str] = set()
            if isinstance(data, list):
                for m in data:
                    # The tx that CREATED this UTXO (address received funds) is the
                    # match's top-level transaction_id — `created_at` only carries
                    # {slot_no, header_hash}, no transaction_id.
                    if m.get("transaction_id"):
                        tx_hashes.add(m["transaction_id"])
                    # The tx that SPENT it (address sent funds) is in spent_at.
                    spent = m.get("spent_at") or {}
                    if spent.get("transaction_id"):
                        tx_hashes.add(spent["transaction_id"])
            return list(tx_hashes)
        except Exception as e:
            logger.debug("get_address_transactions failed for %s: %s", address[:20], e)
            return []

    async def get_spent_utxos(self, address: str) -> list[OutRef]:
        try:
            # Kupo expects a valueless `?spent` flag; `?spent=` (what
            # params={"spent": ""} produces) is rejected as an invalid filter.
            r = await self._kupo.get(f"/matches/{address}?spent")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            out: list[OutRef] = []
            if isinstance(data, list):
                for m in data:
                    spent_at = m.get("spent_at") or {}
                    spent_tx_hash = spent_at.get("transaction_id", "")
                    if spent_tx_hash:
                        out.append(OutRef(tx_hash=spent_tx_hash, output_index=0))
            return out
        except Exception as e:
            logger.debug("get_spent_utxos failed for %s: %s", address[:20], e)
            return []

    async def get_address_spend_map(self, address: str) -> dict[str, str]:
        """UTXO-precise spend map: consumed-input node_id -> spending tx_hash.

        Kupo's ``?spent`` filter returns each spent UTXO at the address with its
        own ``transaction_id``/``output_index`` (the consumed UTXO) and
        ``spent_at.transaction_id`` (the spender). One request, exact mapping.
        """
        spend_map: dict[str, str] = {}
        try:
            # Kupo expects a valueless `?spent` flag; `?spent=` (what
            # params={"spent": ""} produces) is rejected as an invalid filter.
            r = await self._kupo.get(f"/matches/{address}?spent")
            if r.status_code == 404:
                return spend_map
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                for m in data:
                    spent_at = m.get("spent_at") or {}
                    spender = spent_at.get("transaction_id", "")
                    utxo_tx = m.get("transaction_id", "")
                    utxo_idx = m.get("output_index")
                    if spender and utxo_tx and utxo_idx is not None:
                        nid = f"{utxo_tx}:{int(utxo_idx)}"
                        spend_map[nid] = spender
            return spend_map
        except Exception as e:
            logger.debug("get_address_spend_map failed for %s: %s", address[:20], e)
            return spend_map
