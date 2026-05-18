"""Dataclass models for UTXO tracer."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class OutRef:
    tx_hash: str
    output_index: int

    def node_id(self) -> str:
        return f"{self.tx_hash}:{self.output_index}"

    def __str__(self) -> str:
        return f"{self.tx_hash}#{self.output_index}"

    def __hash__(self) -> int:
        return hash(self.node_id())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OutRef) and self.node_id() == other.node_id()


@dataclass
class Asset:
    policy_id: str  # empty string = lovelace
    asset_name: str
    quantity: int  # Python int = arbitrary precision

    @property
    def is_lovelace(self) -> bool:
        return self.policy_id == ""

    @property
    def unit(self) -> str:
        return "lovelace" if self.is_lovelace else f"{self.policy_id}.{self.asset_name}"


@dataclass
class UTxONode:
    id: str
    out_ref: OutRef
    address: str
    assets: list[Asset]
    datum_hash: Optional[str] = None
    inline_datum: Optional[Any] = None
    script_ref: Optional[str] = None

    @property
    def lovelace(self) -> int:
        for a in self.assets:
            if a.is_lovelace:
                return a.quantity
        return 0

    @property
    def ada(self) -> float:
        return self.lovelace / 1_000_000

    @property
    def address_type(self) -> str:
        """Classify the address as 'wallet', 'script', 'byron', 'stake', or 'unknown'."""
        from .utils import classify_address
        return classify_address(self.address).value


@dataclass
class TransactionEdge:
    id: str
    source: str
    target: str
    direction: str  # 'input' | 'output'
    fee: Optional[int] = None
    tx_hash: Optional[str] = None


@dataclass
class TraceStep:
    out_ref: OutRef
    direction: str
    depth: int
    utxo: Optional[UTxONode] = None
    error: Optional[str] = None
    visited_at: float = field(default_factory=time.time)
    parent_out_ref: Optional[OutRef] = None


@dataclass
class CexInfo:
    name: str
    type: str = "exchange"
    confidence: str = "high"


@dataclass
class TraceResult:
    nodes: list[UTxONode]
    edges: list[TransactionEdge]
    traced_path: list[str]
    start_out_ref: OutRef
    direction: str
    max_depth: int
    cex_findings: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    errors_count: int = 0
    steps: list[TraceStep] = field(default_factory=list)
    provider_name: str = ""
