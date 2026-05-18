"""CEX detection + cashflow reconciliation package."""
from .api import (
    BinanceClient,
    BybitClient,
    CexApiClient,
    KuCoinClient,
    OKXClient,
    build_cex_client,
    build_cex_client_from_config,
    get_available_exchanges,
)
from .flow import CashflowReconciler
from .matching import (
    CashflowMatcher,
    ConsolidationPattern,
    detect_consolidations,
    match_batch_withdrawals,
    mcmf_match,
)
from .matching.registry_populate import auto_register_from_consolidation, auto_register_from_matches
from .models import CashflowMatch, CashflowSummary, CexRecord, OnChainRecord
from .registry import (
    get_all_cex_addresses,
    identify_cex,
    load_cex_from_file,
    register_cex_address,
)

__all__ = [
    # Address registry (existing)
    "identify_cex",
    "register_cex_address",
    "load_cex_from_file",
    "get_all_cex_addresses",
    # CEX API
    "CexApiClient",
    "BinanceClient",
    "BybitClient",
    "KuCoinClient",
    "OKXClient",
    "build_cex_client",
    "build_cex_client_from_config",
    "get_available_exchanges",
    # Data models
    "CexRecord",
    "OnChainRecord",
    "CashflowMatch",
    "CashflowSummary",
    # Matching
    "CashflowMatcher",
    "mcmf_match",
    "match_batch_withdrawals",
    "ConsolidationPattern",
    "detect_consolidations",
    "auto_register_from_matches",
    "auto_register_from_consolidation",
    # Orchestration
    "CashflowReconciler",
]
