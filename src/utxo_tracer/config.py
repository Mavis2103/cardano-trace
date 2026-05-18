"""Config management. Stored at ~/.utxo-tracer/config.json.

Priority (highest→lowest): CLI args > env vars (.env) > config.json > defaults

Supported env vars:
  UTXO_TRACER_PROVIDER          default provider name
  BLOCKFROST_API_KEY
  BLOCKFROST_AUTH_TYPE          project_id | bearer | dmtr-api-key
  BLOCKFROST_BASE_URL
  BLOCKFROST_ENDPOINT_URL       Demeter endpoint URL
  KOIOS_API_KEY
  KOIOS_BASE_URL
  MAESTRO_API_KEY
  MAESTRO_BASE_URL
  KUPO_URL
  KUPO_API_KEY
  OGMIOS_URL
  OGMIOS_API_KEY
|  UTxORPC_API_KEY                    UTxORPC API key (optional)
|  UTxORPC_BASE_URL                   UTxORPC base URL (default: mainnet.utxorpc.com)
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".utxo-tracer"
CONFIG_PATH = CONFIG_DIR / "config.json"

_ENV_LOADED = False


def _load_dotenv(dotenv_path: Path | None = None) -> None:
    """Load .env file from given path or search cwd → parents → home."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    candidates: list[Path] = []
    if dotenv_path:
        candidates.append(dotenv_path)
    else:
        # Walk cwd up to root, then home
        p = Path.cwd()
        while True:
            candidates.append(p / ".env")
            if p.parent == p:
                break
            p = p.parent
        candidates.append(Path.home() / ".utxo-tracer" / ".env")

    dotenv_keys: set[str] = set()

    for path in candidates:
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        # Strip 'export ' prefix if present
                        if line.startswith("export "):
                            line = line[7:]
                        key, _, val = line.partition("=")
                        key = key.strip()
                        # Strip inline comments before processing quotes
                        val = val.split("#")[0] if "#" in val else val
                        val = val.strip().strip('"').strip("'")
                        dotenv_keys.add(key)
                        if key and key not in os.environ:
                            os.environ[key] = val
            except Exception:
                pass

            # Warn about env vars in shell that override .env values
            _warn_overridden_env_vars(dotenv_keys)

            break
    _ENV_LOADED = True


_ENV_OVERRIDE_VARS = [
    "UTXO_TRACER_PROVIDER",
    "UTXORPC_API_KEY", "UTXORPC_BASE_URL", "UTXORPC_ENDPOINT_URL",
    "BLOCKFROST_API_KEY", "BLOCKFROST_BASE_URL", "BLOCKFROST_ENDPOINT_URL",
    "KOIOS_API_KEY", "KOIOS_BASE_URL",
    "MAESTRO_API_KEY", "MAESTRO_BASE_URL",
    # CEX env vars
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
    "BYBIT_API_KEY", "BYBIT_API_SECRET",
    "KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE",
    "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE",
]


def _warn_overridden_env_vars(dotenv_keys: set[str]) -> None:
    """Warn if any key env vars are set in the shell, overriding .env values."""
    overridden = [k for k in _ENV_OVERRIDE_VARS if k in os.environ and k not in dotenv_keys]
    if overridden:
        logger.warning(
            "The following env vars are set in your shell and override "
            ".env values (run 'unset %s' to use .env instead): %s",
            " ".join(overridden),
            ", ".join(overridden),
        )


def _env_overlay(base: dict[str, Any]) -> dict[str, Any]:
    """Apply env vars on top of base config dict."""
    cfg = copy.deepcopy(base)

    def _set(path: list[str], val: str | None) -> None:
        if val is None:
            return
        node = cfg
        for key in path[:-1]:
            if not isinstance(node, dict):
                return
            node = node.setdefault(key, {})
        node[path[-1]] = val

    _set(["default_provider"], os.getenv("UTXO_TRACER_PROVIDER"))
    _set(["providers", "blockfrost", "api_key"], os.getenv("BLOCKFROST_API_KEY"))
    _set(["providers", "blockfrost", "auth_type"], os.getenv("BLOCKFROST_AUTH_TYPE"))
    _set(["providers", "blockfrost", "base_url"], os.getenv("BLOCKFROST_BASE_URL"))
    _set(
        ["providers", "blockfrost", "endpoint_url"],
        os.getenv("BLOCKFROST_ENDPOINT_URL"),
    )
    _set(["providers", "koios", "api_key"], os.getenv("KOIOS_API_KEY"))
    _set(["providers", "koios", "base_url"], os.getenv("KOIOS_BASE_URL"))
    _set(["providers", "maestro", "api_key"], os.getenv("MAESTRO_API_KEY"))
    _set(["providers", "maestro", "base_url"], os.getenv("MAESTRO_BASE_URL"))
    _set(["providers", "utxorpc", "api_key"], os.getenv("UTXORPC_API_KEY"))
    _set(["providers", "utxorpc", "base_url"], os.getenv("UTXORPC_BASE_URL"))
    _set(["providers", "utxorpc", "endpoint_url"], os.getenv("UTXORPC_ENDPOINT_URL"))
    _set(["providers", "kupmios", "kupo_url"], os.getenv("KUPO_URL"))
    _set(["providers", "kupmios", "kupo_api_key"], os.getenv("KUPO_API_KEY"))
    _set(["providers", "kupmios", "ogmios_url"], os.getenv("OGMIOS_URL"))
    _set(["providers", "kupmios", "ogmios_api_key"], os.getenv("OGMIOS_API_KEY"))
    # CEX env vars
    _set(["cex", "binance", "api_key"], os.getenv("BINANCE_API_KEY"))
    _set(["cex", "binance", "api_secret"], os.getenv("BINANCE_API_SECRET"))
    _set(["cex", "bybit", "api_key"], os.getenv("BYBIT_API_KEY"))
    _set(["cex", "bybit", "api_secret"], os.getenv("BYBIT_API_SECRET"))
    _set(["cex", "kucoin", "api_key"], os.getenv("KUCOIN_API_KEY"))
    _set(["cex", "kucoin", "api_secret"], os.getenv("KUCOIN_API_SECRET"))
    _set(["cex", "kucoin", "api_passphrase"], os.getenv("KUCOIN_API_PASSPHRASE"))
    _set(["cex", "okx", "api_key"], os.getenv("OKX_API_KEY"))
    _set(["cex", "okx", "api_secret"], os.getenv("OKX_API_SECRET"))
    _set(["cex", "okx", "api_passphrase"], os.getenv("OKX_API_PASSPHRASE"))
    return cfg


DEFAULT_CONFIG: dict[str, Any] = {
    "default_provider": None,
    "providers": {
        "blockfrost": {
            "api_key": None,
            "auth_type": "project_id",
            "base_url": None,
            "endpoint_url": None,
        },
        "koios": {"api_key": None, "base_url": None},
        "maestro": {"api_key": None, "base_url": None},
        "utxorpc": {
            "api_key": None,
            "base_url": None,
            "endpoint_url": None,
        },
        "kupmios": {
            "kupo_url": None,
            "ogmios_url": None,
            "kupo_api_key": None,
            "ogmios_api_key": None,
        },
    },
    "defaults": {
        "max_depth": 5,
        "direction": "backward",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(dotenv_path: Path | None = None) -> dict[str, Any]:
    """Load config: defaults → config.json → env vars (.env). Returns merged dict."""
    _load_dotenv(dotenv_path)
    if not CONFIG_PATH.exists():
        base = copy.deepcopy(DEFAULT_CONFIG)
    else:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = _deep_merge(DEFAULT_CONFIG, data)
        except Exception:
            base = copy.deepcopy(DEFAULT_CONFIG)
    return _env_overlay(base)


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=False)
    tmp.rename(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def clear_config() -> bool:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        return True
    return False


def set_provider_config(
    provider: str,
    api_key: str | None = None,
    base_url: str | None = None,
    auth_type: str | None = None,
    endpoint_url: str | None = None,
    kupo_url: str | None = None,
    ogmios_url: str | None = None,
    kupo_api_key: str | None = None,
    ogmios_api_key: str | None = None,
    make_default: bool = True,
) -> dict[str, Any]:
    # Load raw config.json WITHOUT env overlay to avoid saving env values
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), json.load(f))
        except Exception:
            cfg = copy.deepcopy(DEFAULT_CONFIG)
    else:
        cfg = copy.deepcopy(DEFAULT_CONFIG)

    if provider == "utxorpc":
        if provider not in cfg["providers"]:
            cfg["providers"][provider] = {}
        p = cfg["providers"][provider]
        if api_key is not None:
            p["api_key"] = api_key
        if base_url is not None:
            p["base_url"] = base_url
        if endpoint_url is not None:
            p["endpoint_url"] = endpoint_url
        if make_default:
            cfg["default_provider"] = provider
        save_config(cfg)
        return cfg

    if provider not in cfg["providers"]:
        cfg["providers"][provider] = {}
    p = cfg["providers"][provider]
    if api_key is not None:
        p["api_key"] = api_key
    if base_url is not None:
        p["base_url"] = base_url
    if auth_type is not None:
        p["auth_type"] = auth_type
    if endpoint_url is not None:
        p["endpoint_url"] = endpoint_url
    if kupo_url is not None:
        p["kupo_url"] = kupo_url
    if ogmios_url is not None:
        p["ogmios_url"] = ogmios_url
    if kupo_api_key is not None:
        p["kupo_api_key"] = kupo_api_key
    if ogmios_api_key is not None:
        p["ogmios_api_key"] = ogmios_api_key
    if make_default:
        cfg["default_provider"] = provider
    save_config(cfg)
    return cfg
