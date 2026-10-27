# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31", "pyyaml>=6.0"]
# ///
"""loinc_tool.py — Standalone CLI wrapper for the LOINC Search API.

Each LOINC endpoint is exposed as an argparse subcommand. Results are printed as
JSON to stdout. On error, a JSON error object is printed and the process exits with
status 1.

Credentials are read from the finder's config.yaml (override with --config) under the
keys loinc.username and loinc.password, else from LOINC_USERNAME / LOINC_PASSWORD.
${ENV_VAR} substitution is supported and a .env file next to config.yaml is loaded
automatically.

API docs: https://loinc.org/kb/api/search-api

Usage:
    python loinc_tool.py --help
    python loinc_tool.py search-loinc-codes --query "glucose"
    python loinc_tool.py --config /path/to/config.yaml get-loinc-details --loinc-code 2345-7
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://loinc.regenstrief.org/searchapi"
# The same config file as vocab_tool.py.
DEFAULT_CONFIG = Path(
    os.environ.get("OMOP_CODESET_FINDER_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "omop-codeset-finder"
    / "config.yaml"
)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} patterns with environment variable values."""
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _interpolate(obj: Any) -> Any:
    """Recursively interpolate env vars in string values."""
    if isinstance(obj, str):
        return _interpolate_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate(item) for item in obj]
    return obj


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env without overriding the real environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _load_config(config_path: str) -> Dict[str, Any]:
    config_file = Path(config_path).expanduser().resolve()
    _load_dotenv(config_file.parent / ".env")
    with open(config_file, "r") as fh:
        raw = yaml.safe_load(fh)
    return _interpolate(raw or {})


def _get_auth(cfg: Dict[str, Any]) -> Tuple[str, str] | None:
    """Extract LOINC credentials from config dict. Returns (username, password) or None."""
    loinc_cfg = cfg.get("loinc") or {}
    username = loinc_cfg.get("username") or os.environ.get("LOINC_USERNAME", "")
    password = loinc_cfg.get("password") or os.environ.get("LOINC_PASSWORD", "")
    if not username or not password:
        return None
    return (username, password)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _call_loinc(
    endpoint: str,
    params: Dict[str, Any] | None,
    auth: Tuple[str, str] | None,
) -> Dict[str, Any]:
    """GET the LOINC Search API endpoint and return the JSON response.

    If auth is None, returns an error dict immediately.
    """
    if auth is None:
        return {
            "error": (
                "Missing LOINC credentials. Set loinc.username and loinc.password "
                "in config.yaml (or LOINC_USERNAME / LOINC_PASSWORD env vars)."
            ),
            "endpoint": endpoint,
            "params": params or {},
        }

    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    try:
        resp = requests.get(url, params=params or {}, auth=auth, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        LOGGER.warning("LOINC request failed for %s params=%s: %s", url, params, exc)
        return {"error": str(exc), "url": url, "params": params or {}}


def _first_result(payload: Dict[str, Any]) -> Dict[str, Any] | None:
    """Find the first dict element across the common result-list keys."""
    for key in ("results", "result", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                return first
    return None


# ---------------------------------------------------------------------------
# Handler functions — one per subcommand
# ---------------------------------------------------------------------------

def _handle_search_loinc_codes(args: argparse.Namespace, auth: Tuple[str, str] | None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"query": args.query, "rows": args.rows, "offset": args.offset}
    if args.sortorder:
        params["sortorder"] = args.sortorder
    return _call_loinc("loincs", params, auth)


def _handle_get_loinc_details(args: argparse.Namespace, auth: Tuple[str, str] | None) -> Dict[str, Any]:
    payload = _call_loinc("loincs", {"query": args.loinc_code, "rows": 1, "offset": 0}, auth)
    if payload.get("error"):
        return payload

    result = _first_result(payload)
    if result is None:
        return {
            "error": "No results returned for loinc_code.",
            "loinc_code": args.loinc_code,
            "raw": payload,
        }
    return {"loinc_code": args.loinc_code, "result": result, "raw": payload}


def _handle_search_loinc_parts(args: argparse.Namespace, auth: Tuple[str, str] | None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"query": args.query, "rows": args.rows, "offset": args.offset}
    if args.sortorder:
        params["sortorder"] = args.sortorder
    return _call_loinc("parts", params, auth)


def _handle_search_loinc_answer_lists(
    args: argparse.Namespace, auth: Tuple[str, str] | None
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"query": args.query, "rows": args.rows, "offset": args.offset}
    if args.sortorder:
        params["sortorder"] = args.sortorder
    return _call_loinc("answerlists", params, auth)


def _handle_search_loinc_groups(args: argparse.Namespace, auth: Tuple[str, str] | None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"query": args.query, "rows": args.rows, "offset": args.offset}
    if args.sortorder:
        params["sortorder"] = args.sortorder
    return _call_loinc("groups", params, auth)


def _handle_get_loinc_hierarchy(args: argparse.Namespace, auth: Tuple[str, str] | None) -> Dict[str, Any]:
    # Reuse details logic
    details = _handle_get_loinc_details(args, auth)
    if details.get("error"):
        return details

    result = details.get("result")
    if not isinstance(result, dict):
        return {
            "error": "Unexpected response format for LOINC details.",
            "loinc_code": args.loinc_code,
        }

    hierarchy = (
        result.get("multiaxialHierarchy")
        or result.get("multiaxial")
        or result.get("hierarchy")
        or result.get("parents")
        or result.get("children")
    )
    if hierarchy is None:
        return {
            "error": "No hierarchy fields found in Search API response for this code.",
            "loinc_code": args.loinc_code,
            "available_keys": sorted(result.keys()),
        }
    return {"loinc_code": args.loinc_code, "hierarchy": hierarchy, "raw": result}


def _handle_get_loinc_panels(args: argparse.Namespace, auth: Tuple[str, str] | None) -> Dict[str, Any]:
    # Reuse details logic
    details = _handle_get_loinc_details(args, auth)
    if details.get("error"):
        return details

    result = details.get("result")
    if not isinstance(result, dict):
        return {
            "error": "Unexpected response format for LOINC details.",
            "loinc_code": args.loinc_code,
        }

    panels = (
        result.get("panels")
        or result.get("panelMembers")
        or result.get("members")
        or result.get("panel")
    )
    if panels is None:
        return {
            "error": "No panel fields found in Search API response for this code.",
            "loinc_code": args.loinc_code,
            "available_keys": sorted(result.keys()),
        }
    return {"loinc_code": args.loinc_code, "panels": panels, "raw": result}


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loinc_tool",
        description="Standalone CLI wrapper for the LOINC Search API. Outputs JSON to stdout.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        metavar="PATH",
        help=f"config.yaml with loinc.username and loinc.password (default: {DEFAULT_CONFIG})",
    )

    sub = parser.add_subparsers(dest="command", metavar="SUBCOMMAND")
    sub.required = True

    # 1. search-loinc-codes
    p = sub.add_parser(
        "search-loinc-codes",
        help="Free-text search across LOINC codes.",
    )
    p.add_argument("--query", required=True, metavar="Q",
                   help='Search string (e.g., "glucose", "hemoglobin a1c")')
    p.add_argument("--rows", type=int, default=50, metavar="N",
                   help="Number of rows to return (default: 50)")
    p.add_argument("--offset", type=int, default=0, metavar="N",
                   help="Offset for paging (default: 0)")
    p.add_argument("--sortorder", metavar="S",
                   help="Optional API sort order (field name)")
    p.set_defaults(handler=_handle_search_loinc_codes)

    # 2. get-loinc-details
    p = sub.add_parser(
        "get-loinc-details",
        help="Fetch metadata for a specific LOINC code.",
    )
    p.add_argument("--loinc-code", required=True, metavar="C",
                   help="LOINC code to look up (e.g., 2345-7)")
    p.set_defaults(handler=_handle_get_loinc_details)

    # 3. search-loinc-parts
    p = sub.add_parser(
        "search-loinc-parts",
        help="Search LOINC parts (component, property, system, method, etc.).",
    )
    p.add_argument("--query", required=True, metavar="Q", help="Search string")
    p.add_argument("--rows", type=int, default=50, metavar="N",
                   help="Number of rows to return (default: 50)")
    p.add_argument("--offset", type=int, default=0, metavar="N",
                   help="Offset for paging (default: 0)")
    p.add_argument("--sortorder", metavar="S", help="Optional sort order")
    p.set_defaults(handler=_handle_search_loinc_parts)

    # 4. search-loinc-answer-lists
    p = sub.add_parser(
        "search-loinc-answer-lists",
        help="Find answer lists for assessments/forms.",
    )
    p.add_argument("--query", required=True, metavar="Q", help="Search string")
    p.add_argument("--rows", type=int, default=50, metavar="N",
                   help="Number of rows to return (default: 50)")
    p.add_argument("--offset", type=int, default=0, metavar="N",
                   help="Offset for paging (default: 0)")
    p.add_argument("--sortorder", metavar="S", help="Optional sort order")
    p.set_defaults(handler=_handle_search_loinc_answer_lists)

    # 5. search-loinc-groups
    p = sub.add_parser(
        "search-loinc-groups",
        help="Search LOINC groups (logical groupings).",
    )
    p.add_argument("--query", required=True, metavar="Q", help="Search string")
    p.add_argument("--rows", type=int, default=50, metavar="N",
                   help="Number of rows to return (default: 50)")
    p.add_argument("--offset", type=int, default=0, metavar="N",
                   help="Offset for paging (default: 0)")
    p.add_argument("--sortorder", metavar="S", help="Optional sort order")
    p.set_defaults(handler=_handle_search_loinc_groups)

    # 6. get-loinc-hierarchy
    p = sub.add_parser(
        "get-loinc-hierarchy",
        help="Navigate parent/child relationships via multiaxial hierarchy.",
    )
    p.add_argument("--loinc-code", required=True, metavar="C",
                   help="LOINC code to look up hierarchy for")
    p.set_defaults(handler=_handle_get_loinc_hierarchy)

    # 7. get-loinc-panels
    p = sub.add_parser(
        "get-loinc-panels",
        help="Retrieve panel composition (tests in a panel).",
    )
    p.add_argument("--loinc-code", required=True, metavar="C",
                   help="LOINC code to look up panel members for")
    p.set_defaults(handler=_handle_get_loinc_panels)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run one subcommand and return its JSON-able result (used by mcp_server.py)."""
    args = build_parser().parse_args(argv)
    # The config is optional: LOINC_USERNAME / LOINC_PASSWORD in the environment also work.
    config = Path(args.config).expanduser()
    try:
        cfg = _load_config(str(config)) if config.is_file() else {}
    except Exception as exc:
        return {"error": f"Failed to load config {config}: {exc}"}
    try:
        return args.handler(args, _get_auth(cfg))
    except Exception as exc:
        return {"error": str(exc), "command": args.command}


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    result = run()

    print(json.dumps(result, indent=2), flush=True)

    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
