# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1", "pyyaml>=6.0", "psycopg[binary]>=3.1"]
# ///
"""configure.py — write the OMOP Codeset Finder config.

The config goes to ~/.config/omop-codeset-finder/config.yaml (or $OMOP_CODESET_FINDER_CONFIG,
or --output). Two decisions shape it:

  1. Where the vocabulary is: an Athena download folder (--backend duckdb --vocab-dir) or an
     OMOP Postgres database (--backend postgres --host ... --vocab-schema).
  2. Whether to connect patient data, the OMOP CDM event tables (--cdm-dir or --cdm-schema).
     Connected data lets the tools count patients and flag concepts that have records;
     --only-with-patient yes also keeps only those concepts in every search.

Interactive (asks for everything, including the password):
    python tools/configure.py

Non-interactive (Athena files, vocabulary only):
    python tools/configure.py --backend duckdb --vocab-dir ~/athena

Non-interactive (Postgres with a CDM schema, patient filter on):
    python tools/configure.py --backend postgres --host db.example.org --dbname omop \\
        --user me --vocab-schema vocab --cdm-schema cdm --only-with-patient yes

Passwords are written to .env next to config.yaml (mode 600), never into config.yaml. Without
one, the Postgres driver also reads PGPASSWORD and ~/.pgpass. After writing, `vocab_tool.py
check` is run to validate the setup.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab_tool  # noqa: E402
from vocab_tool import ToolError  # noqa: E402

DEFAULT_CACHE = "~/.cache/omop-codeset-finder/omop.duckdb"


def _ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def _ask_bool(prompt: str, default: bool) -> bool:
    value = _ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return value.startswith("y")


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.strip().lower() in ("1", "y", "yes", "true", "on")


def _write_env(path: Path, updates: Dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    keys = set(updates)
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in keys]
    kept += [f"{k}={v}" for k, v in updates.items()]
    path.write_text("\n".join(kept) + "\n")
    path.chmod(0o600)


def build_config(
    args: argparse.Namespace, interactive: bool
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    env: Dict[str, str] = {}
    backend = args.backend or (
        _ask(
            "Where is the vocabulary? 'duckdb' = Athena download folder (CSV/Parquet), "
            "'postgres' = OMOP database",
            "duckdb",
        )
        if interactive
        else "duckdb"
    )
    backend = "postgres" if backend.lower() == "postgresql" else backend.lower()
    if backend not in ("duckdb", "postgres"):
        raise ToolError(f"Unknown backend: {backend}")

    def val(name: str, prompt: str, default: Optional[str]) -> Optional[str]:
        given = getattr(args, name)
        if given is not None:
            return str(given)
        return _ask(prompt, default) if interactive else default

    if backend == "duckdb":
        vocab_dir = val("vocab_dir", "Folder with the Athena files (CONCEPT.csv, ...)", None)
        if not vocab_dir:
            raise ToolError("--vocab-dir is required for the duckdb backend")
        cdm_arg, cdm_prompt, cdm_default = "cdm_dir", "Folder with the CDM tables", None
    else:
        pg: Dict[str, Any] = {
            "host": val("host", "Postgres host", "localhost"),
            "port": int(val("port", "Postgres port", "5432") or 5432),
            "dbname": val("dbname", "Database name", "omop"),
            "user": val("user", "User", getpass.getuser()),
            "password": "${OMOP_DB_PASSWORD}",
            "vocab_schema": val(
                "vocab_schema", "Vocabulary schema (concept, concept_ancestor, ...)", "vocab"
            ),
        }
        for key in ("sslmode", "sslrootcert"):
            if getattr(args, key):
                pg[key] = getattr(args, key)
        password = args.password
        if password is None and interactive:
            password = getpass.getpass(
                "Password (stored in .env as OMOP_DB_PASSWORD; empty to skip): "
            )
        if password:
            env["OMOP_DB_PASSWORD"] = password
        cdm_arg, cdm_prompt, cdm_default = "cdm_schema", "CDM schema", "cdm"

    # Patient data: connecting it and filtering by it are separate choices.
    cdm = getattr(args, cdm_arg)
    only_with_patient = _parse_bool(args.only_with_patient)
    if (
        cdm is None
        and interactive
        and _ask_bool(
            "Connect patient data (OMOP CDM tables such as condition_occurrence)? "
            "It lets the tools count patients per concept",
            False,
        )
    ):
        cdm = _ask(f"{cdm_prompt} (condition_occurrence, drug_exposure, ...)", cdm_default)
    if cdm and only_with_patient is None and interactive:
        only_with_patient = _ask_bool(
            "Return only concepts that occur in your patient data by default?", True
        )
    if only_with_patient and not cdm:
        raise ToolError(
            "--only-with-patient yes needs patient data: --cdm-dir (duckdb) or "
            "--cdm-schema (postgres)"
        )

    cfg: Dict[str, Any] = {"backend": backend, "only_with_patient": bool(only_with_patient)}
    if backend == "duckdb":
        cache = val(
            "cache_db", "DuckDB cache file (empty = scan the files every call)", DEFAULT_CACHE
        )
        cfg["duckdb"] = {
            "vocab_dir": str(Path(vocab_dir).expanduser()),
            "vocab_delimiter": args.vocab_delimiter,
            "cdm_dir": str(Path(cdm).expanduser()) if cdm else None,
            "cdm_delimiter": "auto",
            "cache_db": cache or None,
        }
    else:
        pg["cdm_schema"] = cdm or None
        pg["statement_timeout_s"] = 120
        cfg["postgres"] = pg

    cfg["loinc"] = {"username": "${LOINC_USERNAME}", "password": "${LOINC_PASSWORD}"}
    if interactive and _ask_bool(
        "Add LOINC Search API credentials now (optional, for lab concepts)?", False
    ):
        env["LOINC_USERNAME"] = _ask("LOINC username")
        env["LOINC_PASSWORD"] = getpass.getpass("LOINC password: ")
    return cfg, env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configure", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--output",
        default=str(vocab_tool.resolve_config_path(None)),
        help="Config file to write (default: %(default)s)",
    )
    parser.add_argument(
        "--backend",
        choices=["duckdb", "postgres"],
        help="duckdb = Athena vocabulary files; postgres = OMOP database",
    )
    parser.add_argument(
        "--only-with-patient",
        choices=["yes", "no"],
        help="yes = discovery tools return only concepts that occur in the patient data",
    )
    parser.add_argument("--vocab-dir", help="(duckdb) Folder with CONCEPT.csv, ...")
    parser.add_argument(
        "--vocab-delimiter",
        default="\t",
        help="(duckdb) Vocabulary file delimiter: tab for Athena downloads, 'auto' to detect",
    )
    parser.add_argument("--cdm-dir", help="(duckdb) Folder with CDM tables as CSV/Parquet")
    parser.add_argument(
        "--cache-db", help=f"(duckdb) Cache file (default {DEFAULT_CACHE}; '' to disable)"
    )
    parser.add_argument("--host", help="(postgres) Host")
    parser.add_argument("--port", type=int, help="(postgres) Port (default 5432)")
    parser.add_argument("--dbname", help="(postgres) Database name")
    parser.add_argument("--user", help="(postgres) User")
    parser.add_argument("--password", help="(postgres) Password, written to .env")
    parser.add_argument("--sslmode", help="(postgres) e.g. require, verify-full")
    parser.add_argument("--sslrootcert", help="(postgres) CA certificate for verify-full")
    parser.add_argument("--vocab-schema", help="(postgres) Schema with concept, ...")
    parser.add_argument("--cdm-schema", help="(postgres) Schema with condition_occurrence, ...")
    parser.add_argument("--no-check", action="store_true", help="Skip running vocab_tool check")
    parser.add_argument(
        "--overwrite",
        "--yes",
        dest="overwrite",
        action="store_true",
        help="Replace an existing config file",
    )
    return parser


def write_config(args: argparse.Namespace, interactive: bool) -> Dict[str, Any]:
    out = Path(args.output).expanduser().resolve()
    if out.exists() and not args.overwrite:
        if not interactive or not _ask_bool(f"{out} exists. Overwrite?", False):
            raise ToolError(f"{out} exists; pass --overwrite to replace it")
    cfg, env = build_config(args, interactive)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Generated by tools/configure.py — see config.example.yaml for all options.\n"
        + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    )
    result: Dict[str, Any] = {"config": str(out)}
    if env:
        _write_env(out.parent / ".env", env)
        result["env"] = str(out.parent / ".env")
    if not args.no_check:
        try:
            result["check"] = vocab_tool.run(["--config", str(out), "check"])
        except Exception as exc:  # report a failed check; the config is still written
            result["check"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


def run(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """Non-interactive configure; returns what was written and the check (mcp_server.py)."""
    return write_config(build_parser().parse_args(argv), interactive=False)


def main() -> None:
    args = build_parser().parse_args()
    interactive = sys.stdin.isatty() and args.backend is None
    try:
        result = write_config(args, interactive)
    except ToolError as exc:
        sys.exit(str(exc))
    print(f"Wrote {result['config']}")
    if "env" in result:
        print(f"Wrote secrets to {result['env']}")
    if "check" in result:
        print(json.dumps(result["check"], indent=2, default=str))
        if "error" in result["check"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
