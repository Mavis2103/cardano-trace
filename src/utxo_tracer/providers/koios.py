"""Koios provider."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..models import Asset, OutRef, UTxONode
from ..utils import hex_to_utf8
from .base import Provider

_LOGGER = logging.getLogger(__name__)


class KoiosProvider(Provider):
    provider_type = "koios"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.koios.rest/api/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        try:
            r = await self._client.post("/tip", json={})
            return r.status_code == 200
        except Exception:
            return False

    def _parse_utxo(self, item: dict) -> UTxONode:
        tx_hash = item.get("tx_hash", "")
        idx = int(item.get("tx_index") or 0)
        out_ref = OutRef(tx_hash=tx_hash, output_index=idx)
        address = ""
        pa = item.get("payment_addr")
        if isinstance(pa, dict):
            address = pa.get("bech32", "") or ""
        elif isinstance(pa, str):
            address = pa
        if not address:
            address = item.get("address", "") or item.get("stake_addr", "") or ""

        assets: list[Asset] = []
        lovelace = int(item.get("value") or 0)
        assets.append(Asset(policy_id="", asset_name="", quantity=lovelace))
        for a in item.get("asset_list", []) or []:
            policy_id = a.get("policy_id", "")
            asset_name_hex = a.get("asset_name", "") or ""
            asset_name = hex_to_utf8(asset_name_hex) if asset_name_hex else ""
            qty = int(a.get("quantity") or 0)
            assets.append(
                Asset(policy_id=policy_id, asset_name=asset_name, quantity=qty)
            )

        inline_datum = item.get("inline_datum")
        ref_script = item.get("reference_script")
        script_ref = None
        if isinstance(ref_script, dict):
            script_ref = ref_script.get("hash") or ref_script.get("bytes")
        elif isinstance(ref_script, str):
            script_ref = ref_script

        return UTxONode(
            id=out_ref.node_id(),
            out_ref=out_ref,
            address=address,
            assets=assets,
            datum_hash=item.get("datum_hash"),
            inline_datum=inline_datum,
            script_ref=script_ref,
        )

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        try:
            body = {
                "_utxo_refs": [f"{out_ref.tx_hash}#{out_ref.output_index}"],
                "_extended": True,
            }
            r = await self._client.post("/utxo_info", json=body)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            arr = r.json()
            if not arr:
                return None
            return self._parse_utxo(arr[0])
        except Exception:
            return None

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        try:
            body = {"_tx_hashes": [tx_hash]}
            r = await self._client.post("/tx_info", json=body)
            if r.status_code == 404:
                return {"inputs": [], "outputs": []}
            r.raise_for_status()
            arr = r.json()
            if not arr:
                return {"inputs": [], "outputs": []}
            if len(arr) > 1:
                _LOGGER.warning(
                    "Koios tx_info returned %d txs for hash %s, using first",
                    len(arr),
                    tx_hash,
                )
            tx = arr[0]
            inputs: list[OutRef] = []
            input_utxos: dict[str, UTxONode] = {}
            for i in tx.get("inputs", []) or []:
                th = i.get("tx_hash") or (i.get("out_ref") or {}).get("tx_hash")
                ti = i.get("tx_index")
                if ti is None:
                    ti = (i.get("out_ref") or {}).get("tx_index", 0)
                if th is not None:
                    out_ref = OutRef(tx_hash=th, output_index=int(ti or 0))
                    inputs.append(out_ref)
                    # Koios tx_info returns full input details — cache the UTXO
                    utxo = self._parse_utxo(i)
                    input_utxos[out_ref.node_id()] = utxo

            outputs: list[UTxONode] = []
            for o in tx.get("outputs", []) or []:
                # ensure tx_hash present
                if not o.get("tx_hash"):
                    o = {**o, "tx_hash": tx_hash}
                outputs.append(self._parse_utxo(o))
            return {"inputs": inputs, "input_utxos": input_utxos, "outputs": outputs}
        except Exception:
            return {"inputs": [], "outputs": []}

    async def get_tx_block_time(self, tx_hash: str) -> int | None:
        """Fetch block time for a transaction from Koios."""
        try:
            body = {"_tx_hashes": [tx_hash]}
            r = await self._client.post("/tx_info", json=body)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            arr = r.json()
            if not arr:
                return None
            return arr[0].get("block_time")
        except Exception:
            return None

    async def get_spent_utxos(self, address: str) -> list[OutRef]:
        """Find transactions that spent UTXOs from this address (forward tracing).

        Uses Koios /address_txs to find all TXs involving the address,
        then checks which ones have this address as input.

        Returns list of OutRefs for the spent outputs.
        """
        try:
            body = {"_addresses": [address]}
            r = await self._client.post("/address_txs", json=body)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            txs = r.json()
        except Exception:
            return []

        spent_refs: list[OutRef] = []
        seen_tx_hashes: set[str] = set()

        for tx in txs:
            tx_hash = tx.get("tx_hash", "")
            if not tx_hash or tx_hash in seen_tx_hashes:
                continue
            seen_tx_hashes.add(tx_hash)

            try:
                tx_data = await self.get_transaction_utxos(tx_hash)
                for inp in tx_data.get("inputs", []):
                    if inp.tx_hash:
                        spent_refs.append(inp)
            except Exception:
                continue

        return spent_refs
