# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""Standalone CLI wrapper for the RxClass REST API.

Each RxClass endpoint is exposed as an argparse subcommand. Results are printed as
JSON to stdout. On error, a JSON error object is printed and the process exits with
status 1.

Usage:
    python rxclass_tool.py --help
    python rxclass_tool.py find-class-by-name --class-name "ACE Inhibitors"
    python rxclass_tool.py get-class-members --class-id A10BK
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://rxnav.nlm.nih.gov/REST/rxclass"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _call_rxclass(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{BASE_URL}/{path}.json"
    try:
        resp = requests.get(url, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        LOGGER.warning("RxClass request failed for %s params=%s: %s", url, params, exc)
        return {"error": str(exc), "url": url, "params": params or {}}


# ---------------------------------------------------------------------------
# Handler functions — one per subcommand
# ---------------------------------------------------------------------------

def _handle_find_class_by_name(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"className": args.class_name}
    if args.class_types:
        params["classTypes"] = " ".join(args.class_types)
    return _call_rxclass("class/byName", params)


def _handle_find_classes_by_id(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"classId": args.class_id}
    return _call_rxclass("class/byId", params)


def _handle_find_similar_classes_by_class(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "classId": args.class_id,
        "relaSource": args.rela_source,
        "rela": args.rela,
    }
    if args.score_type:
        params["scoreType"] = args.score_type
    if args.top is not None:
        params["top"] = args.top
    return _call_rxclass("class/similar", params)


def _handle_find_similar_classes_by_drug_list(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"rxcuis": " ".join(args.rxcuis)}
    if args.rela_source:
        params["relaSource"] = args.rela_source
    if args.rela:
        params["rela"] = args.rela
    return _call_rxclass("class/similarByRxcuis", params)


def _handle_get_all_classes(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if args.class_types:
        params["classTypes"] = " ".join(args.class_types)
    return _call_rxclass("allClasses", params)


def _handle_get_class_by_rxcui(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"rxcui": args.rxcui}
    if args.rela_source:
        params["relaSource"] = args.rela_source
    if args.relas:
        params["relas"] = " ".join(args.relas)
    return _call_rxclass("class/byRxcui", params)


def _handle_get_class_by_drug_name(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"drugName": args.drug_name}
    if args.rela_source:
        params["relaSource"] = args.rela_source
    if args.relas:
        params["relas"] = " ".join(args.relas)
    return _call_rxclass("class/byDrugName", params)


def _handle_get_class_contexts(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"classId": args.class_id}
    return _call_rxclass("classContext", params)


def _handle_get_class_graph(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"classId": args.class_id}
    if args.source:
        params["source"] = args.source
    return _call_rxclass("classGraph", params)


def _handle_get_class_members(args: argparse.Namespace) -> Dict[str, Any]:
    rela_sources = ["ATC", "ATCPROD", "CDC", "DAILYMED", "FMTSME", "MEDRT", "RXNORM", "SNOMEDCT", "VA"]
    base_params: Dict[str, Any] = {"classId": args.class_id}
    if args.rela:
        base_params["rela"] = args.rela
    if args.trans:
        base_params["trans"] = args.trans
    if args.ttys:
        base_params["ttys"] = " ".join(args.ttys)

    members: List[Dict[str, Any]] = []
    seen_rxcuis: set[str] = set()
    sources_with_results: List[str] = []

    for rela_source in rela_sources:
        params = dict(base_params)
        params["relaSource"] = rela_source
        response = _call_rxclass("classMembers", params)
        if "rxclassDrugMembers" in response:
            items = response["rxclassDrugMembers"].get("rxclassDrugMember", [])
            for item in items:
                rxcui = item.get("minConcept", {}).get("rxcui")
                if rxcui and rxcui not in seen_rxcuis:
                    seen_rxcuis.add(rxcui)
                    members.append(item)
            if items:
                sources_with_results.append(rela_source)

    return {
        "class_id": args.class_id,
        "sources_queried": rela_sources,
        "sources_with_results": sources_with_results,
        "member_count": len(members),
        "members": members,
    }


def _handle_get_class_tree(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"classId": args.class_id}
    if args.class_type:
        params["classType"] = args.class_type
    return _call_rxclass("classTree", params)


def _handle_get_class_types(args: argparse.Namespace) -> Dict[str, Any]:
    return _call_rxclass("classTypes", {})


def _handle_get_rela_source_version(args: argparse.Namespace) -> Dict[str, Any]:
    return _call_rxclass(f"version/{args.rela_source}", {})


def _handle_get_relas(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if args.rela_source:
        params["relaSource"] = args.rela_source
    return _call_rxclass("relas", params)


def _handle_get_similarity_information(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "classId1": args.class_id1,
        "relaSource1": args.rela_source1,
        "rela1": args.rela1,
        "classId2": args.class_id2,
        "relaSource2": args.rela_source2,
        "rela2": args.rela2,
    }
    return _call_rxclass("class/similarInfo", params)


def _handle_get_sources_of_drug_class_relations(args: argparse.Namespace) -> Dict[str, Any]:
    return _call_rxclass("relaSources", {})


def _handle_get_spelling_suggestions(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"term": args.term}
    if args.type:
        params["type"] = args.type
    return _call_rxclass("spellingsuggestions", params)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rxclass_tool",
        description="Standalone CLI wrapper for the RxClass REST API. Outputs JSON to stdout.",
    )
    sub = parser.add_subparsers(dest="command", metavar="SUBCOMMAND")
    sub.required = True

    # 1. find-class-by-name
    p = sub.add_parser(
        "find-class-by-name",
        help="Search for drug classes by text name (not code/ID).",
    )
    p.add_argument("--class-name", required=True, metavar="NAME",
                   help="Descriptive name to search (e.g., 'ACE Inhibitors')")
    p.add_argument("--class-types", nargs="+", metavar="T",
                   help="Optional type filter (ATC1-4, CHEM, EPC, MOA, VA, ...)")
    p.set_defaults(handler=_handle_find_class_by_name)

    # 2. find-classes-by-id
    p = sub.add_parser(
        "find-classes-by-id",
        help="Lookup class metadata by class ID/code.",
    )
    p.add_argument("--class-id", required=True, metavar="ID",
                   help="Class identifier/code (e.g., 'A10', 'A10BA02', 'HS502')")
    p.set_defaults(handler=_handle_find_classes_by_id)

    # 3. find-similar-classes-by-class
    p = sub.add_parser(
        "find-similar-classes-by-class",
        help="Find classes similar to a given class.",
    )
    p.add_argument("--class-id", required=True, metavar="ID", help="Source class ID")
    p.add_argument("--rela-source", required=True, metavar="S", help="Relation source (e.g., ATC)")
    p.add_argument("--rela", required=True, metavar="R", help="Relation type")
    p.add_argument("--score-type", metavar="T", help="Score type (optional)")
    p.add_argument("--top", type=int, metavar="N", help="Return top N results (optional)")
    p.set_defaults(handler=_handle_find_similar_classes_by_class)

    # 4. find-similar-classes-by-drug-list
    p = sub.add_parser(
        "find-similar-classes-by-drug-list",
        help="Find classes similar to a specified list of RxCUIs.",
    )
    p.add_argument("--rxcuis", required=True, nargs="+", metavar="R",
                   help="One or more RxCUI values")
    p.add_argument("--rela-source", metavar="S", help="Relation source (optional)")
    p.add_argument("--rela", metavar="R", help="Relation type (optional)")
    p.set_defaults(handler=_handle_find_similar_classes_by_drug_list)

    # 5. get-all-classes
    p = sub.add_parser(
        "get-all-classes",
        help="Enumerate all available drug classes.",
    )
    p.add_argument("--class-types", nargs="+", metavar="T",
                   help="Optional class type filter")
    p.set_defaults(handler=_handle_get_all_classes)

    # 6. get-class-by-rxcui
    p = sub.add_parser(
        "get-class-by-rxcui",
        help="Get classes containing a specified drug RxCUI.",
    )
    p.add_argument("--rxcui", required=True, metavar="R", help="RxNorm concept ID")
    p.add_argument("--rela-source", metavar="S", help="Relation source (optional)")
    p.add_argument("--relas", nargs="+", metavar="R", help="Relation types (optional)")
    p.set_defaults(handler=_handle_get_class_by_rxcui)

    # 7. get-class-by-drug-name
    p = sub.add_parser(
        "get-class-by-drug-name",
        help="Find what drug classes contain a specific drug by name.",
    )
    p.add_argument("--drug-name", required=True, metavar="NAME",
                   help="Drug name (e.g., 'metformin', 'sitagliptin')")
    p.add_argument("--rela-source", metavar="S", help="Relation source (optional)")
    p.add_argument("--relas", nargs="+", metavar="R", help="Relation types (optional)")
    p.set_defaults(handler=_handle_get_class_by_drug_name)

    # 8. get-class-contexts
    p = sub.add_parser(
        "get-class-contexts",
        help="Get paths from a class to the root of its hierarchies.",
    )
    p.add_argument("--class-id", required=True, metavar="ID", help="Class ID")
    p.set_defaults(handler=_handle_get_class_contexts)

    # 9. get-class-graph
    p = sub.add_parser(
        "get-class-graph",
        help="Get the graph from a class to the hierarchy root.",
    )
    p.add_argument("--class-id", required=True, metavar="ID", help="Class ID")
    p.add_argument("--source", metavar="S", help="Source filter (optional)")
    p.set_defaults(handler=_handle_get_class_graph)

    # 10. get-class-members
    p = sub.add_parser(
        "get-class-members",
        help="Get all drug members (RxCUIs) belonging to a drug class. Queries all rela sources.",
    )
    p.add_argument("--class-id", required=True, metavar="ID",
                   help="Class code (e.g., 'A10', 'A10BK', 'HS502')")
    p.add_argument("--rela", metavar="R", help="Relation filter (optional)")
    p.add_argument("--trans", metavar="T", help="Transitivity setting (optional)")
    p.add_argument("--ttys", nargs="+", metavar="T", help="Term types filter (optional)")
    p.set_defaults(handler=_handle_get_class_members)

    # 11. get-class-tree
    p = sub.add_parser(
        "get-class-tree",
        help="Get subclasses/descendants of a class.",
    )
    p.add_argument("--class-id", required=True, metavar="ID", help="Class ID")
    p.add_argument("--class-type", metavar="T", help="Class type filter (optional)")
    p.set_defaults(handler=_handle_get_class_tree)

    # 12. get-class-types
    p = sub.add_parser(
        "get-class-types",
        help="List available class types (ATC, EPC, MESHPA, NDFRT, VA, ...).",
    )
    p.set_defaults(handler=_handle_get_class_types)

    # 13. get-rela-source-version
    p = sub.add_parser(
        "get-rela-source-version",
        help="Get dataset version info for a relation source.",
    )
    p.add_argument("--rela-source", required=True, metavar="S",
                   help="Relation source (e.g., ATC, MEDRT, VA)")
    p.set_defaults(handler=_handle_get_rela_source_version)

    # 14. get-relas
    p = sub.add_parser(
        "get-relas",
        help="List relationship types for a rela source.",
    )
    p.add_argument("--rela-source", metavar="S", help="Relation source (optional)")
    p.set_defaults(handler=_handle_get_relas)

    # 15. get-similarity-information
    p = sub.add_parser(
        "get-similarity-information",
        help="Get similarity information between two classes.",
    )
    p.add_argument("--class-id1", required=True, metavar="ID1", help="First class ID")
    p.add_argument("--rela-source1", required=True, metavar="S1",
                   help="Relation source for first class")
    p.add_argument("--rela1", required=True, metavar="R1",
                   help="Relation type for first class")
    p.add_argument("--class-id2", required=True, metavar="ID2", help="Second class ID")
    p.add_argument("--rela-source2", required=True, metavar="S2",
                   help="Relation source for second class")
    p.add_argument("--rela2", required=True, metavar="R2",
                   help="Relation type for second class")
    p.set_defaults(handler=_handle_get_similarity_information)

    # 16. get-sources-of-drug-class-relations
    p = sub.add_parser(
        "get-sources-of-drug-class-relations",
        help="List all sources of drug-class relations (relaSource values).",
    )
    p.set_defaults(handler=_handle_get_sources_of_drug_class_relations)

    # 17. get-spelling-suggestions
    p = sub.add_parser(
        "get-spelling-suggestions",
        help="Get spelling suggestions for drug or class names.",
    )
    p.add_argument("--term", required=True, metavar="T", help="Term to get suggestions for")
    p.add_argument("--type", metavar="T", help="Type filter (optional)")
    p.set_defaults(handler=_handle_get_spelling_suggestions)

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
