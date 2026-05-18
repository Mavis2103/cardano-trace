"""Maestro provider."""

from __future__ import annotations

from typing import Optional

import httpx

from ..models import Asset, OutRef, UTxONode
from ..utils import parse_blockfrost_unit
from .base import Provider


class MaestroProvider(Provider):
    provider_type = "maestro"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://mainnet.gomaestro-api.org/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        try:
            r = await self._client.get("/health")
            if r.status_code == 200:
                return True
            # fallback: try chain-tip
            r2 = await self._client.get("/chain-tip")
            return r2.status_code == 200
        except Exception:
            return False

    def _parse_assets(self, items: list[dict]) -> list[Asset]:
        assets: list[Asset] = []
        for a in items or []:
            unit = a.get("unit", "")
            qty_raw = a.get("amount", a.get("quantity", "0"))
            qty = int(qty_raw or 0)
            policy_id, asset_name = parse_blockfrost_unit(unit)
            assets.append(
                Asset(policy_id=policy_id, asset_name=asset_name, quantity=qty)
            )
        return assets

    def _parse_output(self, tx_hash: str, o: dict) -> UTxONode:
        idx = int(o.get("index") or o.get("output_index") or 0)
        out_ref = OutRef(tx_hash=tx_hash, output_index=idx)
        assets = self._parse_assets(o.get("assets", []))
        datum = o.get("datum") or {}
        datum_hash = None
        inline_datum = None
        if isinstance(datum, dict):
            datum_hash = datum.get("hash")
            if datum.get("type") == "inline":
                inline_datum = datum.get("bytes")
        ref_script = o.get("reference_script") or {}
        script_ref = None
        if isinstance(ref_script, dict):
            script_ref = ref_script.get("hash") or ref_script.get("bytes")
        return UTxONode(
            id=out_ref.node_id(),
            out_ref=out_ref,
            address=o.get("address", ""),
            assets=assets,
            datum_hash=datum_hash,
            inline_datum=inline_datum,
            script_ref=script_ref,
        )

    async def _fetch_tx(self, tx_hash: str) -> Optional[dict]:
        try:
            r = await self._client.get(f"/transactions/{tx_hash}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            # Maestro often wraps under "data"
            if (
                isinstance(data, dict)
                and "data" in data
                and isinstance(data["data"], dict)
            ):
                return data["data"]
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise  # let fallback handle rate-limit
            return None
        except Exception:
            return None

    async def get_utxo_by_out_ref(self, out_ref: OutRef) -> Optional[UTxONode]:
        tx = await self._fetch_tx(out_ref.tx_hash)
        if not tx:
            return None
        outputs = tx.get("outputs", [])
        for o in outputs:
            idx = int(o.get("index", o.get("output_index", -1)))
            if idx == out_ref.output_index:
                return self._parse_output(out_ref.tx_hash, o)
        return None

    async def get_transaction_utxos(self, tx_hash: str) -> dict:
        tx = await self._fetch_tx(tx_hash)
        if not tx:
            return {"inputs": [], "outputs": []}

        inputs: list[OutRef] = []
        input_utxos: dict[str, UTxONode] = {}
        for i in tx.get("inputs", []) or []:
            th = i.get("tx_hash") or i.get("transaction_id")
            ti = i.get("index")
            if ti is None:
                ti = i.get("output_index", 0)
            if th is not None:
                out_ref = OutRef(tx_hash=th, output_index=int(ti or 0))
                inputs.append(out_ref)
                # Maestro returns full input details — cache the UTXO
                utxo = self._parse_output(th, i)
                input_utxos[out_ref.node_id()] = utxo

        outputs: list[UTxONode] = []
        for o in tx.get("outputs", []) or []:
            outputs.append(self._parse_output(tx_hash, o))
        return {"inputs": inputs, "input_utxos": input_utxos, "outputs": outputs}
