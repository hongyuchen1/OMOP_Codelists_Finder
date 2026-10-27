# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""Standalone CLI wrapper for the Disease Ontology REST API.

Each DO endpoint is exposed as an argparse subcommand. Results are printed as
JSON to stdout. On error, a JSON error object is printed and the process exits
with status 1.

Usage:
    python do_tool.py --help
    python do_tool.py search-diseases --names "diabetes mellitus"
    python do_tool.py get-disease-by-id --term-id DOID:9352
    python do_tool.py get-xref-mappings --term-id DOID:9352 --target-vocab ICD10CM
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://api.disease-ontology.org/v1"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _call_do(
    method: str,
    path: str,
    *,
    params: Dict[str, Any] | None = None,
    json_body: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        resp = requests.request(
            method.upper(), url, params=params or {}, json=json_body, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {"data": data}
    except Exception as exc:
        LOGGER.warning("DO request failed for %s %s params=%s: %s", method, url, params, exc)
        return {"error": str(exc), "url": url, "method": method.upper(), "params": params or {}}


# ---------------------------------------------------------------------------
# Core logic — one function per subcommand
# ---------------------------------------------------------------------------


def search_diseases(
    names: Optional[List[str]] = None,
    ids: Optional[List[str]] = None,
    xrefs: Optional[List[str]] = None,
    synonyms: Optional[List[str]] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """Faceted search by name, ID, xref, or synonym."""
    data: Dict[str, Any] = {}
    if names:
        data["names"] = names
    if ids:
        data["ids"] = ids
    if xrefs:
        data["xrefs"] = xrefs
    if synonyms:
        data["synonyms"] = synonyms

    return _call_do(
        "POST",
        "terms/search",
        params={"page": page, "page_size": page_size},
        json_body={"data": data},
    )


def get_disease_by_id(term_id: str) -> Dict[str, Any]:
    """Retrieve a full term by DOID (e.g., DOID:9352)."""
    return _call_do("GET", f"terms/{quote(term_id, safe='')}")


def get_disease_by_label(label: str) -> Dict[str, Any]:
    """Retrieve a term by human-readable name (label)."""
    return _call_do("GET", f"terms/label/{quote(label, safe='')}")


def list_diseases(page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    """Paginated listing of all disease terms."""
    return _call_do("GET", "terms", params={"page": page, "page_size": page_size})


def get_xref_mappings(term_id: str, target_vocab: str) -> Dict[str, Any]:
    """Extract cross-references filtered by target vocabulary prefix.

    Example:
        get_xref_mappings("DOID:9352", "ICD10CM") -> {"mappings": ["E11", ...]}
    """
    term = get_disease_by_id(term_id)
    if term.get("error"):
        return term

    xrefs = term.get("xrefs", [])
    if not isinstance(xrefs, list):
        return {"error": "Unexpected xrefs format in term response.", "term_id": term_id}

    target = target_vocab.strip().upper()
    matches: List[str] = []
    for xref in xrefs:
        if not isinstance(xref, str):
            continue
        if ":" not in xref:
            continue
        prefix, code = xref.split(":", 1)
        if prefix.strip().upper() == target and code:
            matches.append(code)

    return {"term_id": term_id, "target_vocab": target_vocab, "mappings": matches, "raw": xrefs}


def get_disease_hierarchy(term_id: str) -> Dict[str, Any]:
    """Return parents + children with DOID + name pairs for ontology traversal."""
    term = get_disease_by_id(term_id)
    if term.get("error"):
        return term

    parents = term.get("parents", [])
    children = term.get("children", [])
    if not isinstance(parents, list) or not isinstance(children, list):
        return {"error": "Unexpected hierarchy format in term response.", "term_id": term_id}

    cache: Dict[str, str | None] = {}

    def _label_for(doid: str) -> str | None:
        if doid in cache:
            return cache[doid]
        payload = get_disease_by_id(doid)
        if payload.get("error"):
            cache[doid] = None
            return None
        name = payload.get("name")
        cache[doid] = name if isinstance(name, str) else None
        return cache[doid]

    parent_items = [{"id": p, "name": _label_for(p)} for p in parents if isinstance(p, str)]
    child_items = [{"id": c, "name": _label_for(c)} for c in children if isinstance(c, str)]

    return {
        "term_id": term_id,
        "name": term.get("name"),
        "parents": parent_items,
        "children": child_items,
    }


def list_imports() -> Dict[str, Any]:
    """List available import ontologies."""
    return _call_do("GET", "imports")


def get_import_terms(import_name: str) -> Dict[str, Any]:
    """Retrieve terms from a specific import."""
    return _call_do("GET", f"imports/{quote(import_name, safe='')}")


# ---------------------------------------------------------------------------
# Handler functions — one per subcommand
# ---------------------------------------------------------------------------


def _handle_search_diseases(args: argparse.Namespace) -> Dict[str, Any]:
    return search_diseases(
        names=args.names,
        ids=args.ids,
        xrefs=args.xrefs,
        synonyms=args.synonyms,
        page=args.page,
        page_size=args.page_size,
    )


def _handle_get_disease_by_id(args: argparse.Namespace) -> Dict[str, Any]:
    return get_disease_by_id(args.term_id)


def _handle_get_disease_by_label(args: argparse.Namespace) -> Dict[str, Any]:
    return get_disease_by_label(args.label)


def _handle_list_diseases(args: argparse.Namespace) -> Dict[str, Any]:
    return list_diseases(page=args.page, page_size=args.page_size)


def _handle_get_xref_mappings(args: argparse.Namespace) -> Dict[str, Any]:
    return get_xref_mappings(args.term_id, args.target_vocab)


def _handle_get_disease_hierarchy(args: argparse.Namespace) -> Dict[str, Any]:
    return get_disease_hierarchy(args.term_id)


def _handle_list_imports(args: argparse.Namespace) -> Dict[str, Any]:
    return list_imports()


def _handle_get_import_terms(args: argparse.Namespace) -> Dict[str, Any]:
    return get_import_terms(args.import_name)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="do_tool",
        description="Standalone CLI wrapper for the Disease Ontology REST API. Outputs JSON to stdout.",
    )
    sub = parser.add_subparsers(dest="command", metavar="SUBCOMMAND")
    sub.required = True

    # 1. search-diseases
    p = sub.add_parser(
        "search-diseases",
        help="Faceted search by name, ID, xref, or synonym.",
    )
    p.add_argument("--names", nargs="+", metavar="N", help="One or more disease names to search")
    p.add_argument("--ids", nargs="+", metavar="I", help="One or more DOID identifiers to search")
    p.add_argument("--xrefs", nargs="+", metavar="X", help="One or more cross-reference strings")
    p.add_argument("--synonyms", nargs="+", metavar="S", help="One or more synonym strings")
    p.add_argument("--page", type=int, default=1, metavar="P", help="Page number (default: 1)")
    p.add_argument(
        "--page-size", type=int, default=20, dest="page_size", metavar="N",
        help="Results per page (default: 20)",
    )
    p.set_defaults(handler=_handle_search_diseases)

    # 2. get-disease-by-id
    p = sub.add_parser(
        "get-disease-by-id",
        help="Retrieve a full disease term by DOID (e.g., DOID:9352).",
    )
    p.add_argument("--term-id", required=True, dest="term_id", metavar="ID",
                   help="DOID identifier (e.g., DOID:9352)")
    p.set_defaults(handler=_handle_get_disease_by_id)

    # 3. get-disease-by-label
    p = sub.add_parser(
        "get-disease-by-label",
        help="Retrieve a disease term by human-readable label.",
    )
    p.add_argument("--label", required=True, metavar="L",
                   help="Human-readable disease name (e.g., 'diabetes mellitus')")
    p.set_defaults(handler=_handle_get_disease_by_label)

    # 4. list-diseases
    p = sub.add_parser(
        "list-diseases",
        help="Paginated listing of all disease terms.",
    )
    p.add_argument("--page", type=int, default=1, metavar="P", help="Page number (default: 1)")
    p.add_argument(
        "--page-size", type=int, default=20, dest="page_size", metavar="N",
        help="Results per page (default: 20)",
    )
    p.set_defaults(handler=_handle_list_diseases)

    # 5. get-xref-mappings
    p = sub.add_parser(
        "get-xref-mappings",
        help="Extract cross-references for a DOID filtered by target vocabulary prefix.",
    )
    p.add_argument("--term-id", required=True, dest="term_id", metavar="ID",
                   help="DOID identifier (e.g., DOID:9352)")
    p.add_argument("--target-vocab", required=True, dest="target_vocab", metavar="V",
                   help="Target vocabulary prefix (e.g., ICD10CM, ICD9CM, MESH, OMIM)")
    p.set_defaults(handler=_handle_get_xref_mappings)

    # 6. get-disease-hierarchy
    p = sub.add_parser(
        "get-disease-hierarchy",
        help="Return parents and children with DOID + name pairs for a disease term.",
    )
    p.add_argument("--term-id", required=True, dest="term_id", metavar="ID",
                   help="DOID identifier (e.g., DOID:9352)")
    p.set_defaults(handler=_handle_get_disease_hierarchy)

    # 7. list-imports
    p = sub.add_parser(
        "list-imports",
        help="List available import ontologies.",
    )
    p.set_defaults(handler=_handle_list_imports)

    # 8. get-import-terms
    p = sub.add_parser(
        "get-import-terms",
        help="Retrieve terms from a specific import ontology.",
    )
    p.add_argument("--import-name", required=True, dest="import_name", metavar="NAME",
                   help="Import name as returned by list-imports")
    p.set_defaults(handler=_handle_get_import_terms)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run one subcommand and return its JSON-able result (used by mcp_server.py)."""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
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
