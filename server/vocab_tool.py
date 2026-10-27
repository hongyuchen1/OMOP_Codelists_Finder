# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1", "pyyaml>=6.0", "psycopg[binary]>=3.1"]
# ///
"""vocab_tool.py — OMOP vocabulary query CLI with DuckDB and Postgres backends.

Backends (chosen by `backend:` in config.yaml):
  duckdb    Query Athena vocabulary CSV/Parquet files (and optional CDM CSV/Parquet
            files) directly. Optionally materializes them into a local .duckdb cache.
  postgres  Query an OMOP database (vocabulary schema + optional CDM schema).

When `only_with_patient: true`, every discovery command (search, code lookup,
descendants, ancestors, mappings, ...) keeps only concepts that appear in the linked CDM
event tables. Validation/metadata commands are never filtered.

The config is --config, else $OMOP_CODESET_FINDER_CONFIG, else
~/.config/omop-codeset-finder/config.yaml (written by configure.py).

Every command prints a single JSON object to stdout. Errors print
{"error": ...} and exit with status 1.

Usage:
    python vocab_tool.py [--config PATH] [--patient-filter on|off] <subcommand> [args]
    python vocab_tool.py check
    python vocab_tool.py search-concepts --query "type 2 diabetes" --vocab-list SNOMED ICD10CM
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

CONFIG_ENV_VAR = "OMOP_CODESET_FINDER_CONFIG"
# Outside the skill folder, so plugin updates and reinstalls keep it.
DEFAULT_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "omop-codeset-finder"
    / "config.yaml"
)
SETUP_HINT = (
    "Not configured. Ask the user, never guessing paths, hosts or schemas: (1) where the "
    "vocabulary is: a folder with the Athena download (backend duckdb; needs CONCEPT.csv) or an "
    "OMOP Postgres database (backend postgres: host, port, database, user, vocabulary schema, "
    "and sslmode if the server needs TLS); (2) whether to also connect patient data, the OMOP "
    "CDM tables (a folder of CDM CSV/Parquet files, or the CDM schema); (3) if so, whether "
    "searches return only concepts that occur in it (only_with_patient). Then call `configure` "
    "with the answers. Passwords never go through the chat: the user puts OMOP_DB_PASSWORD=... "
    "in the .env file next to the config, or uses ~/.pgpass or PGPASSWORD."
)

VOCAB_TABLES = ("concept", "concept_synonym", "concept_ancestor", "concept_relationship")

# domain_id (upper) -> (CDM table, concept columns checked for patient data)
DEFAULT_DOMAIN_CDM_MAP: Dict[str, Tuple[str, List[str]]] = {
    "CONDITION": ("condition_occurrence", ["condition_concept_id", "condition_source_concept_id"]),
    "DRUG": ("drug_exposure", ["drug_concept_id", "drug_source_concept_id"]),
    "MEASUREMENT": ("measurement", ["measurement_concept_id", "measurement_source_concept_id"]),
    "PROCEDURE": (
        "procedure_occurrence",
        ["procedure_concept_id", "procedure_source_concept_id"],
    ),
    "OBSERVATION": ("observation", ["observation_concept_id", "observation_source_concept_id"]),
    "DEVICE": ("device_exposure", ["device_concept_id", "device_source_concept_id"]),
}

CONCEPT_COLS = (
    "concept_id, concept_name, domain_id, vocabulary_id, concept_class_id, "
    "standard_concept, concept_code, valid_start_date, valid_end_date, invalid_reason"
)


class ToolError(Exception):
    """User-facing error (printed as JSON, exit 1)."""


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides the real environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _interpolate(obj: Any) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} with environment values."""
    if isinstance(obj, str):
        return _ENV_VAR_RE.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate(item) for item in obj]
    return obj


def resolve_config_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get(CONFIG_ENV_VAR):
        return Path(os.environ[CONFIG_ENV_VAR]).expanduser().resolve()
    return DEFAULT_CONFIG


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.is_file():
        raise ToolError(f"Config not found: {config_path}. {SETUP_HINT}")
    _load_dotenv(config_path.parent / ".env")
    with open(config_path) as fh:
        raw = yaml.safe_load(fh) or {}
    cfg: Dict[str, Any] = _interpolate(raw)
    cfg["_config_dir"] = config_path.parent
    cfg["_config_path"] = config_path
    return cfg


def _resolve_dir(cfg: Dict[str, Any], value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = cfg["_config_dir"] / path
    return path.resolve()


def _domain_map(cfg: Dict[str, Any]) -> Dict[str, Tuple[str, List[str]]]:
    """Default domain→CDM mapping, optionally overridden by `patient_filter.domains`."""
    mapping = dict(DEFAULT_DOMAIN_CDM_MAP)
    overrides = (cfg.get("patient_filter") or {}).get("domains") or {}
    for domain, spec in overrides.items():
        if spec is None:
            mapping.pop(domain.upper(), None)
            continue
        mapping[domain.upper()] = (str(spec["table"]).lower(), [c.lower() for c in spec["columns"]])
    return mapping


# ---------------------------------------------------------------------------
# SQL helpers (backend-neutral; `?` placeholders)
# ---------------------------------------------------------------------------


def _in_list(values: Sequence[Any]) -> Tuple[str, List[Any]]:
    """Return ("(?, ?, ...)", params). An empty list yields a clause matching nothing."""
    values = list(dict.fromkeys(values))
    if not values:
        return "(NULL)", []
    return "(" + ", ".join("?" for _ in values) + ")", values


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_code(value: str) -> str:
    return str(value).strip().replace(" ", "").upper()


# Index-friendly code lookups.
# Comparing UPPER(concept_code) or UPPER(vocabulary_id) defeats the standard OMOP
# indexes (idx_concept_code, idx_concept_vocabulary_id), which made every code lookup
# a scan of the whole CONCEPT table. Codes are compared as stored instead: the code
# as given, in upper and in lower case (a mixed-case code such as UCUM "mg/dL" must be
# given in its own case, as UCUM requires); the vocabulary id is matched case-blind
# through the small VOCABULARY table; a prefix pattern ("E11%") is an index range.


def _code_variants(value: str) -> List[str]:
    """A code as given (spaces removed), in upper and in lower case."""
    raw = str(value).strip().replace(" ", "")
    return list(dict.fromkeys([raw, raw.upper(), raw.lower()]))


def _vocab_match(ctx: "Ctx", value: str, alias: str = "c") -> Tuple[str, List[Any]]:
    """`alias.vocabulary_id` equal to `value` case-blind, in a form an index can serve:
    the stored ids from the VOCABULARY table when the backend has one, else UPPER()."""
    index = getattr(ctx.db, "_vocabulary_ids", None)
    if index is None and ctx.db.has_vocab("vocabulary"):
        index = {}
        for r in ctx.db.query(f"SELECT vocabulary_id FROM {ctx.db.vocab('vocabulary')}"):
            index.setdefault(str(r["vocabulary_id"]).upper(), []).append(str(r["vocabulary_id"]))
        ctx.db._vocabulary_ids = index
    if index is None:
        return f"UPPER({alias}.vocabulary_id) = UPPER(?)", [value]
    placeholders, params = _in_list(index.get(str(value).upper()) or [str(value)])
    return f"{alias}.vocabulary_id IN {placeholders}", params


def _vocab_list_match(ctx: "Ctx", values: Sequence[str], alias: Optional[str] = "c"
                      ) -> Tuple[Optional[str], List[Any]]:
    """`vocabulary_id IN (the stored ids of values)` when the backend has a VOCABULARY table, else (None, [])."""
    ids: List[str] = []
    for v in values:
        sql, params = _vocab_match(ctx, v, alias="x")
        if not sql.startswith("x.vocabulary_id IN"):
            return None, []
        ids += params
    col = f"{alias}.vocabulary_id" if alias else "vocabulary_id"
    placeholders, params = _in_list(ids)
    return f"{col} IN {placeholders}", params


_ALNUM = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _like_prefix(pattern: str) -> Optional[str]:
    """The prefix of a LIKE pattern that is a prefix and nothing more ("E11%" -> "E11")."""
    prefix = pattern.rstrip("%")
    if prefix == pattern or not prefix or any(ch in prefix for ch in "%_\\"):
        return None
    return prefix


def _prefix_upper(prefix: str) -> Optional[str]:
    """A bound that every string starting with `prefix` sorts below, in byte order and in
    the usual locale collations alike: the last letter or digit incremented (9 -> A, a
    trailing Z or z carried to the character before), other trailing characters dropped
    (each step only widens the range). None when nothing is left."""
    p = prefix
    while p:
        last = p[-1]
        if last not in _ALNUM or last in "Zz":
            p = p[:-1]
        elif last == "9":
            return p[:-1] + "A"
        else:
            return p[:-1] + chr(ord(last) + 1)
    return None


def _prefix_ranges(prefix: str, alias: str = "c") -> Optional[Tuple[str, List[Any]]]:
    """`alias.concept_code` in an index range for the prefix as given, in upper and in
    lower case (OR-ed), or None when no range can be formed."""
    parts: List[str] = []
    params: List[Any] = []
    for p in _code_variants(prefix):
        upper = _prefix_upper(p)
        if upper is None:
            return None
        parts.append(f"({alias}.concept_code >= ? AND {alias}.concept_code < ?)")
        params += [p, upper]
    return "(" + " OR ".join(parts) + ")", params


def _tokenize_query(text: str, max_tokens: int = 8) -> List[str]:
    tokens: List[str] = []
    for tok in re.findall(r"[A-Za-z0-9]+", text.lower()):
        if tok not in tokens:
            tokens.append(tok)
        if len(tokens) >= max_tokens:
            break
    return tokens


def _is_int_column(name: str) -> bool:
    return (
        name in ("concept_id", "person_id", "concept_id_1", "concept_id_2")
        or name.endswith("_concept_id")
        or name.endswith("_levels_of_separation")
    )


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class Backend:
    name = "base"

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def vocab(self, table: str) -> str:
        raise NotImplementedError

    def has_vocab(self, table: str) -> bool:
        raise NotImplementedError

    def cdm(self, table: str) -> str:
        raise NotImplementedError

    def cdm_columns(self, table: str) -> set[str]:
        raise NotImplementedError

    def patient_exists(self, domain: str, table: str, columns: List[str], alias: str) -> str:
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        raise NotImplementedError


class DuckDBBackend(Backend):
    """Athena CSV/Parquet files queried in place (views) or via a materialized cache."""

    name = "duckdb"
    CACHE_VERSION = "1"

    def __init__(self, cfg: Dict[str, Any]):
        try:
            import duckdb  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment specific
            raise ToolError(
                "duckdb is not installed: pip install duckdb (or use `uv run`)"
            ) from exc
        self._duckdb = duckdb
        self.cfg = cfg
        dcfg = cfg.get("duckdb") or {}
        self.vocab_dir = _resolve_dir(cfg, dcfg.get("vocab_dir"))
        self.cdm_dir = _resolve_dir(cfg, dcfg.get("cdm_dir"))
        self.vocab_files: Dict[str, str] = dcfg.get("vocab_files") or {}
        self.cdm_files: Dict[str, str] = dcfg.get("cdm_files") or {}
        self.vocab_delim = dcfg.get("vocab_delimiter", "\t")
        self.cdm_delim = dcfg.get("cdm_delimiter", "auto")
        self.cache_path = _resolve_dir(cfg, dcfg.get("cache_db"))
        self.domain_map = _domain_map(cfg)
        self.need_patient_table = bool(cfg.get("only_with_patient")) or bool(self.cdm_dir)

        if self.vocab_dir is None and not self.vocab_files:
            raise ToolError("duckdb.vocab_dir is not set in config.yaml")

        self.vocab_sources = {
            t: p for t in VOCAB_TABLES if (p := self._find(self.vocab_dir, t, self.vocab_files))
        }
        if "concept" not in self.vocab_sources:
            raise ToolError(
                f"CONCEPT file not found in {self.vocab_dir} (expected CONCEPT.csv or .parquet)"
            )
        cdm_tables = {table for table, _ in self.domain_map.values()}
        self.cdm_sources = {
            t: p
            for t in sorted(cdm_tables)
            if (self.cdm_dir or self.cdm_files)
            and (p := self._find(self.cdm_dir, t, self.cdm_files))
        }
        self._cdm_cols: Dict[str, set[str]] = {}
        self.conn = self._open()

    # -- file discovery -----------------------------------------------------

    @staticmethod
    def _find(base: Optional[Path], table: str, explicit: Dict[str, str]) -> Optional[Path]:
        if table in explicit and explicit[table]:
            path = Path(str(explicit[table])).expanduser()
            if not path.is_absolute() and base is not None:
                path = base / path
            return path.resolve() if path.exists() else None
        if base is None or not base.is_dir():
            return None
        wanted = {
            f"{table}{ext}" for ext in (".csv", ".tsv", ".txt", ".parquet", ".csv.gz", ".tsv.gz")
        }
        for child in sorted(base.iterdir()):
            if child.name.lower() in wanted:
                return child.resolve()
        return None

    @staticmethod
    def _sql_str(text: str) -> str:
        return "'" + str(text).replace("'", "''") + "'"

    def _reader(self, path: Path, delim: Optional[str]) -> str:
        if path.suffix.lower() == ".parquet":
            return f"read_parquet({self._sql_str(str(path))})"
        opts = ["header=true", "all_varchar=true"]
        if delim and delim != "auto":
            # Athena exports are unquoted TSV; names may contain '"', so disable quoting.
            opts += [f"delim={self._sql_str(delim)}", "quote=''", "escape=''"]
        return f"read_csv({self._sql_str(str(path))}, {', '.join(opts)})"

    def _typed_select(
        self, conn: Any, path: Path, delim: Optional[str], keep: Optional[set] = None
    ) -> str:
        reader = self._reader(path, delim)
        cols = [row[0] for row in conn.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()]
        parts = []
        for col in cols:
            lname = col.lower()
            if keep is not None and lname not in keep:
                continue
            quoted = '"' + col.replace('"', '""') + '"'
            if _is_int_column(lname):
                parts.append(f"TRY_CAST({quoted} AS BIGINT) AS {lname}")
            else:
                parts.append(f"CAST({quoted} AS VARCHAR) AS {lname}")
        return f"SELECT {', '.join(parts)} FROM {reader}"

    # -- connection / cache -------------------------------------------------

    def _signature(self) -> str:
        payload = {
            "version": self.CACHE_VERSION,
            "vocab": {
                t: [str(p), p.stat().st_mtime, p.stat().st_size]
                for t, p in self.vocab_sources.items()
            },
            "cdm": {
                t: [str(p), p.stat().st_mtime, p.stat().st_size]
                for t, p in self.cdm_sources.items()
            },
            "delims": [self.vocab_delim, self.cdm_delim],
            "domains": {k: [v[0], v[1]] for k, v in self.domain_map.items()},
            "patient_table": self.need_patient_table,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _populate(self, conn: Any, materialize: bool) -> None:
        kind = "TABLE" if materialize else "VIEW"
        for table, path in self.vocab_sources.items():
            conn.execute(
                f"CREATE OR REPLACE {kind} {table} AS {self._typed_select(conn, path, self.vocab_delim)}"
            )
        # CDM tables stay as views over the files (they can be very large).
        for table, path in self.cdm_sources.items():
            conn.execute(
                f"CREATE OR REPLACE VIEW cdm_{table} AS {self._typed_select(conn, path, self.cdm_delim)}"
            )
        self._cdm_cols = {}
        branches = []
        for domain, (table, columns) in self.domain_map.items():
            for col in sorted(self._columns_of(conn, table) & set(columns)):
                branches.append(
                    f"SELECT DISTINCT '{domain}' AS domain, {col} AS concept_id "
                    f"FROM cdm_{table} WHERE {col} IS NOT NULL AND {col} <> 0"
                )
        if branches and self.need_patient_table:
            body = " UNION ".join(branches)
            conn.execute(f"CREATE OR REPLACE {kind} _patient_concepts AS {body}")

    def _columns_of(self, conn: Any, table: str) -> set[str]:
        if table not in self.cdm_sources:
            return set()
        if table not in self._cdm_cols:
            rows = conn.execute(f"DESCRIBE SELECT * FROM cdm_{table}").fetchall()
            self._cdm_cols[table] = {r[0].lower() for r in rows}
        return self._cdm_cols[table]

    def _open(self) -> Any:
        duckdb = self._duckdb
        if self.cache_path is None:
            conn = duckdb.connect(":memory:")
            self._populate(conn, materialize=False)
            return conn

        signature = self._signature()
        if self.cache_path.exists():
            try:
                conn = duckdb.connect(str(self.cache_path), read_only=True)
                row = conn.execute(
                    "SELECT value FROM _cache_meta WHERE key = 'signature'"
                ).fetchone()
                if row and row[0] == signature:
                    return conn
                conn.close()
            except Exception:
                pass
        self.build_cache(signature)
        return duckdb.connect(str(self.cache_path), read_only=True)

    def build_cache(self, signature: Optional[str] = None) -> None:
        """Materialize vocab tables (+ patient concept index) into cache_db atomically."""
        if self.cache_path is None:
            raise ToolError("duckdb.cache_db is not set in config.yaml")
        signature = signature or self._signature()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_name(f"{self.cache_path.name}.tmp-{os.getpid()}")
        if tmp.exists():
            tmp.unlink()
        print(
            f"[vocab_tool] building DuckDB cache at {self.cache_path} (one-time; may take minutes)",
            file=sys.stderr,
        )
        conn = self._duckdb.connect(str(tmp))
        try:
            self._populate(conn, materialize=True)
            conn.execute("CREATE TABLE _cache_meta (key VARCHAR, value VARCHAR)")
            conn.execute("INSERT INTO _cache_meta VALUES ('signature', ?)", [signature])
            conn.close()
            os.replace(tmp, self.cache_path)
        finally:
            if tmp.exists():
                tmp.unlink()

    # -- Backend API ------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        cur = self.conn.execute(sql, list(params))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def has_vocab(self, table: str) -> bool:
        return table in self.vocab_sources

    def vocab(self, table: str) -> str:
        if not self.has_vocab(table):
            raise ToolError(f"Vocabulary table '{table}' not found in {self.vocab_dir}")
        return table

    def cdm(self, table: str) -> str:
        if table not in self.cdm_sources:
            raise ToolError(f"CDM table '{table}' not found in {self.cdm_dir}")
        return f"cdm_{table}"

    def cdm_columns(self, table: str) -> set[str]:
        return self._columns_of(self.conn, table)

    def patient_exists(self, domain: str, table: str, columns: List[str], alias: str) -> str:
        return (
            f"EXISTS (SELECT 1 FROM _patient_concepts p "
            f"WHERE p.domain = '{domain}' AND p.concept_id = {alias}.concept_id)"
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self.name,
            "vocab_dir": str(self.vocab_dir) if self.vocab_dir else None,
            "vocab_tables": {t: str(p) for t, p in self.vocab_sources.items()},
            "cdm_dir": str(self.cdm_dir) if self.cdm_dir else None,
            "cdm_tables": {t: str(p) for t, p in self.cdm_sources.items()},
            "cache_db": str(self.cache_path) if self.cache_path else None,
        }


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class PostgresBackend(Backend):
    name = "postgres"

    def __init__(self, cfg: Dict[str, Any]):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - environment specific
            raise ToolError(
                "psycopg is not installed: pip install 'psycopg[binary]' (or use `uv run`)"
            ) from exc
        pcfg = cfg.get("postgres") or {}
        self.vocab_schema = str(pcfg.get("vocab_schema") or "public")
        self.cdm_schema = str(pcfg.get("cdm_schema") or "") or None
        for schema in filter(None, [self.vocab_schema, self.cdm_schema]):
            if not _IDENT_RE.match(schema):
                raise ToolError(f"Invalid schema name: {schema!r}")
        self.domain_map = _domain_map(cfg)
        timeout_ms = int(pcfg.get("statement_timeout_s", 120)) * 1000
        conninfo = {
            k: pcfg[k]
            for k in ("host", "port", "dbname", "user", "password", "sslmode", "sslrootcert")
            if pcfg.get(k)
        }
        try:
            self.conn = psycopg.connect(
                **conninfo,
                connect_timeout=int(pcfg.get("connect_timeout_s", 10)),
                options=f"-c statement_timeout={timeout_ms}",
                autocommit=True,
                row_factory=dict_row,
            )
        except Exception as exc:
            safe = {k: v for k, v in conninfo.items() if k != "password"}
            raise ToolError(f"Postgres connection failed ({safe}): {exc}") from exc
        self._tables = self._list_columns()

    def _list_columns(self) -> Dict[Tuple[str, str], set[str]]:
        schemas = [s for s in {self.vocab_schema, self.cdm_schema} if s]
        placeholders, params = _in_list(schemas)
        rows = self.query(
            "SELECT table_schema, table_name, column_name FROM information_schema.columns "
            f"WHERE table_schema IN {placeholders}",
            params,
        )
        tables: Dict[Tuple[str, str], set[str]] = {}
        for row in rows:
            key = (row["table_schema"], row["table_name"].lower())
            tables.setdefault(key, set()).add(row["column_name"].lower())
        return tables

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), list(params))
            return list(cur.fetchall()) if cur.description else []

    def has_vocab(self, table: str) -> bool:
        return (self.vocab_schema, table) in self._tables

    def vocab(self, table: str) -> str:
        if not self.has_vocab(table):
            raise ToolError(f"Table {self.vocab_schema}.{table} not found")
        return f"{self.vocab_schema}.{table}"

    def cdm(self, table: str) -> str:
        if not self.cdm_schema or (self.cdm_schema, table) not in self._tables:
            raise ToolError(
                f"CDM table {self.cdm_schema}.{table} not found (set postgres.cdm_schema)"
            )
        return f"{self.cdm_schema}.{table}"

    def cdm_columns(self, table: str) -> set[str]:
        if not self.cdm_schema:
            return set()
        return self._tables.get((self.cdm_schema, table), set())

    def patient_exists(self, domain: str, table: str, columns: List[str], alias: str) -> str:
        ref = self.cdm(table)
        return (
            "("
            + " OR ".join(
                f"EXISTS (SELECT 1 FROM {ref} t WHERE t.{col} = {alias}.concept_id)"
                for col in columns
            )
            + ")"
        )

    def describe(self) -> Dict[str, Any]:
        info = self.conn.info
        return {
            "backend": self.name,
            "host": info.host,
            "port": info.port,
            "dbname": info.dbname,
            "user": info.user,
            "vocab_schema": self.vocab_schema,
            "vocab_tables": [t for t in VOCAB_TABLES if self.has_vocab(t)],
            "cdm_schema": self.cdm_schema,
            "cdm_tables": sorted({t for (s, t) in self._tables if s == self.cdm_schema}),
        }


def make_backend(cfg: Dict[str, Any]) -> Backend:
    backend = str(cfg.get("backend", "duckdb")).lower()
    if backend == "duckdb":
        return DuckDBBackend(cfg)
    if backend in ("postgres", "postgresql"):
        return PostgresBackend(cfg)
    raise ToolError(f"Unknown backend {backend!r} (expected 'duckdb' or 'postgres')")


# ---------------------------------------------------------------------------
# Context: backend + patient filter
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, cfg: Dict[str, Any], backend: Backend, patient_filter: bool):
        self.cfg = cfg
        self.db = backend
        self.patient_filter = patient_filter
        self.domain_map = _domain_map(cfg)
        self._linked: Optional[Dict[str, Tuple[str, List[str]]]] = None

    def linked_domains(self) -> Dict[str, Tuple[str, List[str]]]:
        """Domains whose CDM table (and at least one concept column) is available."""
        if self._linked is None:
            linked = {}
            for domain, (table, columns) in self.domain_map.items():
                available = [c for c in columns if c in self.db.cdm_columns(table)]
                if available:
                    linked[domain] = (table, available)
            self._linked = linked
        return self._linked

    def patient_clause(self, alias: str = "c") -> str:
        """SQL predicate keeping concepts with patient data (TRUE when filter is off).

        Concepts whose domain has no linked CDM table pass through unfiltered.
        """
        if not self.patient_filter:
            return "TRUE"
        linked = self.linked_domains()
        if not linked:
            raise ToolError(
                "only_with_patient is enabled but no CDM event tables are linked. Point "
                "duckdb.cdm_dir / postgres.cdm_schema at your CDM data, or rerun with "
                "--patient-filter off."
            )
        parts = [
            f"(UPPER({alias}.domain_id) = '{domain}' AND "
            f"{self.db.patient_exists(domain, table, cols, alias)})"
            for domain, (table, cols) in linked.items()
        ]
        names = ", ".join(f"'{d}'" for d in linked)
        parts.append(f"(UPPER({alias}.domain_id) NOT IN ({names}))")
        return "(" + " OR ".join(parts) + ")"

    def meta(self) -> Dict[str, Any]:
        return {"backend": self.db.name, "only_with_patient": self.patient_filter}


def _concept_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "concept_id": int(row["concept_id"]),
        "concept_name": row.get("concept_name"),
        "domain_id": row.get("domain_id"),
        "vocabulary_id": row.get("vocabulary_id"),
        "concept_class_id": row.get("concept_class_id"),
        "standard_concept": row.get("standard_concept") or None,
        "concept_code": row.get("concept_code"),
        "invalid_reason": row.get("invalid_reason") or None,
    }
    for key in ("valid_start_date", "valid_end_date"):
        if row.get(key) is not None:
            out[key] = str(row[key])
    return out


def _vocab_filter(vocab_list: Optional[List[str]], alias: str = "c") -> Tuple[str, List[Any]]:
    if not vocab_list:
        return "TRUE", []
    placeholders, params = _in_list([v.upper() for v in vocab_list])
    return f"UPPER({alias}.vocabulary_id) IN {placeholders}", params


def _output(data: Any) -> None:
    print(json.dumps(data, default=str, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_check(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    concept = ctx.db.vocab("concept")
    n = ctx.db.query(f"SELECT COUNT(*) AS n FROM {concept}")[0]["n"]
    vocabs = ctx.db.query(f"SELECT COUNT(DISTINCT vocabulary_id) AS n FROM {concept}")[0]["n"]
    linked = ctx.linked_domains()
    warnings = []
    if ctx.patient_filter and not linked:
        warnings.append(
            "only_with_patient is on but no CDM tables are linked; discovery commands will fail."
        )
    for table in VOCAB_TABLES:
        if not ctx.db.has_vocab(table):
            warnings.append(
                f"Optional vocabulary table '{table}' is missing; related commands will fail."
            )
    return {
        "ok": True,
        "configured": True,
        "config": str(ctx.cfg["_config_path"]),
        **ctx.db.describe(),
        "concept_rows": int(n),
        "vocabulary_count": int(vocabs),
        "only_with_patient": ctx.patient_filter,
        "patient_linked_domains": {d: {"table": t, "columns": c} for d, (t, c) in linked.items()},
        "warnings": warnings,
    }


def cmd_build_cache(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    if not isinstance(ctx.db, DuckDBBackend):
        raise ToolError("build-cache only applies to the duckdb backend")
    ctx.db.conn.close()
    ctx.db.build_cache()
    return {"ok": True, "cache_db": str(ctx.db.cache_path)}


def cmd_list_vocabularies(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    rows = ctx.db.query(
        f"SELECT vocabulary_id, COUNT(*) AS n FROM {ctx.db.vocab('concept')} "
        "GROUP BY vocabulary_id ORDER BY vocabulary_id"
    )
    return {
        "vocabularies": [r["vocabulary_id"] for r in rows],
        "concept_counts": {r["vocabulary_id"]: int(r["n"]) for r in rows},
    }


def cmd_list_vocabularies_by_table(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    table = args.table.lower()
    columns = next((cols for t, cols in ctx.domain_map.values() if t == table), None)
    if columns is None:
        raise ToolError(f"Unknown CDM table: {table}")
    columns = [c for c in columns if c in ctx.db.cdm_columns(table)]
    if not columns:
        return {"table": table, "vocabularies": [], "warning": f"CDM table '{table}' not linked"}
    ref = ctx.db.cdm(table)
    ids = " UNION ".join(f"SELECT {c} AS concept_id FROM {ref}" for c in columns)
    rows = ctx.db.query(
        f"SELECT DISTINCT c.vocabulary_id FROM {ctx.db.vocab('concept')} c "
        f"JOIN ({ids}) t ON t.concept_id = c.concept_id ORDER BY c.vocabulary_id"
    )
    return {"table": table, "columns": columns, "vocabularies": [r["vocabulary_id"] for r in rows]}


def cmd_list_domains_by_vocabulary(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    vsql, vparams = _vocab_match(ctx, args.vocabulary, alias="c")
    rows = ctx.db.query(
        f"SELECT DISTINCT c.domain_id FROM {ctx.db.vocab('concept')} c "
        f"WHERE {vsql} ORDER BY c.domain_id",
        vparams,
    )
    return {"vocabulary_id": args.vocabulary, "domains": [r["domain_id"] for r in rows]}


def cmd_search_concepts(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    query: str = args.query.strip()
    if not query:
        raise ToolError("--query must not be empty")
    top_k = max(50, args.top_k or 50)
    use_synonyms = not args.no_synonyms and ctx.db.has_vocab("concept_synonym")
    concept = ctx.db.vocab("concept")

    def name_match(col: str) -> Tuple[str, List[Any]]:
        if args.fuzzy:
            tokens = _tokenize_query(query) or [query.lower()]
            return (
                " AND ".join(f"{col} ILIKE ? ESCAPE '\\'" for _ in tokens),
                [f"%{_like_escape(t)}%" for t in tokens],
            )
        return f"{col} ILIKE ? ESCAPE '\\'", [f"%{_like_escape(query)}%"]

    main_sql, main_params = name_match("concept_name")
    hits = f"SELECT concept_id FROM {concept} WHERE {main_sql}"
    hit_params = list(main_params)
    if args.vocab_list:  # the name scan reads only those vocabularies' rows (an index)
        in_sql, in_params = _vocab_list_match(ctx, args.vocab_list, alias=None)
        if in_sql is not None:
            hits += f" AND {in_sql}"
            hit_params += in_params
    if use_synonyms:
        syn_sql, syn_params = name_match("concept_synonym_name")
        hits += f" UNION SELECT concept_id FROM {ctx.db.vocab('concept_synonym')} WHERE {syn_sql}"
        hit_params += syn_params

    vocab_sql, vocab_params = _vocab_filter(args.vocab_list)
    where = [vocab_sql]
    params: List[Any] = hit_params + vocab_params
    if args.domain_id:
        where.append("UPPER(c.domain_id) = UPPER(?)")
        params.append(args.domain_id)
    if args.standard_only:
        where.append("c.standard_concept = 'S'")
    if not args.include_invalid:
        where.append("(c.invalid_reason IS NULL OR c.invalid_reason = '')")
    where.append(ctx.patient_clause("c"))

    sql = (
        f"SELECT {', '.join('c.' + col.strip() for col in CONCEPT_COLS.split(','))} "
        f"FROM {concept} c JOIN ({hits}) h ON h.concept_id = c.concept_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY CASE WHEN LOWER(c.concept_name) = LOWER(?) THEN 0 "
        "WHEN c.concept_name ILIKE ? ESCAPE '\\' THEN 1 ELSE 2 END, "
        "LENGTH(c.concept_name), c.concept_id LIMIT ?"
    )
    params += [query, f"%{_like_escape(query)}%", top_k]
    items = [_concept_to_dict(r) for r in ctx.db.query(sql, params)]
    return {
        **ctx.meta(),
        "query": query,
        "vocabularies": args.vocab_list,
        "domain_id": args.domain_id,
        "fuzzy": args.fuzzy,
        "synonyms_searched": use_synonyms,
        "count": len(items),
        "items": items,
    }


def cmd_lookup_concept_by_code(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    code = _normalize_code(args.code)
    vsql, vparams = _vocab_match(ctx, args.vocabulary)
    cph, cparams = _in_list(_code_variants(args.code))
    rows = ctx.db.query(
        f"SELECT {CONCEPT_COLS} FROM {ctx.db.vocab('concept')} c "
        f"WHERE c.concept_code IN {cph} AND {vsql} "
        f"AND {ctx.patient_clause('c')} ORDER BY c.concept_id",
        [*cparams, *vparams],
    )
    base = {**ctx.meta(), "vocabulary_id": args.vocabulary, "code": code, "found": bool(rows)}
    if rows:
        base["concept"] = _concept_to_dict(rows[0])
        if len(rows) > 1:
            base["all_matches"] = [_concept_to_dict(r) for r in rows]
    return base


def cmd_search_concepts_by_code_pattern(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    top_k = max(50, args.top_k or 100)
    vsql, vparams = _vocab_match(ctx, args.vocabulary)
    prefix = _like_prefix(args.pattern)
    ranges = _prefix_ranges(prefix) if prefix is not None else None
    rsql, rparams = ranges if ranges is not None else ("TRUE", [])
    rows = ctx.db.query(
        f"SELECT {CONCEPT_COLS} FROM {ctx.db.vocab('concept')} c "
        f"WHERE {rsql} AND {vsql} AND UPPER(c.concept_code) LIKE UPPER(?) "
        f"AND {ctx.patient_clause('c')} ORDER BY c.concept_code LIMIT ?",
        [*rparams, *vparams, args.pattern, top_k],
    )
    items = [_concept_to_dict(r) for r in rows]
    return {
        **ctx.meta(),
        "vocabulary_id": args.vocabulary,
        "pattern": args.pattern,
        "count": len(items),
        "items": items,
    }


def cmd_validate_codes(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    codes = list(dict.fromkeys(_normalize_code(c) for c in args.codes if str(c).strip()))
    concept = ctx.db.vocab("concept")
    vsql, vparams = _vocab_match(ctx, args.vocabulary)
    variants = [v for c in args.codes if str(c).strip() for v in _code_variants(c)]
    placeholders, params = _in_list(variants)
    rows = ctx.db.query(
        f"SELECT DISTINCT c.concept_code AS code FROM {concept} c "
        f"WHERE c.concept_code IN {placeholders} AND {vsql} "
        f"AND {ctx.patient_clause('c')}",
        [*params, *vparams],
    )
    found = {_normalize_code(r["code"]) for r in rows}
    missing = sorted(c for c in codes if c not in found)
    suggestions: Dict[str, List[str]] = {}
    for code in missing:
        ranges = _prefix_ranges(code[:3])
        rsql, rparams = ranges if ranges is not None else ("TRUE", [])
        sugg = ctx.db.query(
            f"SELECT DISTINCT c.concept_code FROM {concept} c "
            f"WHERE {rsql} AND {vsql} AND UPPER(c.concept_code) LIKE ? ESCAPE '\\' "
            f"AND {ctx.patient_clause('c')} ORDER BY c.concept_code LIMIT 5",
            [*rparams, *vparams, _like_escape(code[:3]) + "%"],
        )
        suggestions[code] = [r["concept_code"] for r in sugg]
    return {
        **ctx.meta(),
        "vocabulary_id": args.vocabulary,
        "existing": sorted(found),
        "missing": missing,
        "suggestions": suggestions,
    }


def cmd_validate_concept_ids(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    ids = list(dict.fromkeys(args.concept_ids))
    placeholders, params = _in_list(ids)
    rows = ctx.db.query(
        f"SELECT concept_id FROM {ctx.db.vocab('concept')} WHERE concept_id IN {placeholders}",
        params,
    )
    found = {int(r["concept_id"]) for r in rows}
    return {
        "existing": sorted(i for i in ids if i in found),
        "missing": sorted(i for i in ids if i not in found),
    }


def cmd_codes_to_concept_ids(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    variants = [v for c in args.codes if str(c).strip() for v in _code_variants(c)]
    placeholders, params = _in_list(variants)
    vsql, vparams = _vocab_match(ctx, args.vocabulary)
    rows = ctx.db.query(
        f"SELECT c.concept_id, c.concept_code FROM {ctx.db.vocab('concept')} c "
        f"WHERE c.concept_code IN {placeholders} AND {vsql} "
        f"AND {ctx.patient_clause('c')} ORDER BY c.concept_id",
        [*params, *vparams],
    )
    return {
        **ctx.meta(),
        "vocabulary_id": args.vocabulary,
        "concept_ids": sorted({int(r["concept_id"]) for r in rows}),
        "code_to_concept_ids": _group(rows, "concept_code", "concept_id"),
    }


def _group(rows: Iterable[Dict[str, Any]], key: str, value: str) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for r in rows:
        out.setdefault(str(r[key]), []).append(int(r[value]))
    return out


def cmd_fetch_concept_metadata(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    ids = list(dict.fromkeys(args.concept_ids))
    placeholders, params = _in_list(ids)
    has_patients = ""
    if ctx.linked_domains():
        forced = Ctx(ctx.cfg, ctx.db, patient_filter=True)
        forced._linked = ctx.linked_domains()
        # Unlinked domains pass through the clause, so report them as unknown (NULL).
        linked = ", ".join(f"'{d}'" for d in forced._linked)
        has_patients = (
            f", CASE WHEN UPPER(c.domain_id) NOT IN ({linked}) THEN NULL "
            f"WHEN {forced.patient_clause('c')} THEN TRUE ELSE FALSE END AS has_patient_data"
        )
    rows = ctx.db.query(
        f"SELECT {CONCEPT_COLS}{has_patients} FROM {ctx.db.vocab('concept')} c "
        f"WHERE c.concept_id IN {placeholders} ORDER BY c.concept_id",
        params,
    )
    items = []
    for r in rows:
        item = _concept_to_dict(r)
        if "has_patient_data" in r:
            item["has_patient_data"] = r["has_patient_data"]
        items.append(item)
    if args.include_synonyms and items and ctx.db.has_vocab("concept_synonym"):
        syn = ctx.db.query(
            f"SELECT concept_id, concept_synonym_name FROM {ctx.db.vocab('concept_synonym')} "
            f"WHERE concept_id IN {placeholders}",
            params,
        )
        syn_map: Dict[int, List[str]] = {}
        for r in syn:
            syn_map.setdefault(int(r["concept_id"]), []).append(r["concept_synonym_name"])
        for item in items:
            item["synonyms"] = sorted(set(syn_map.get(item["concept_id"], [])))
    found = {i["concept_id"] for i in items}
    return {"items": items, "missing": [i for i in ids if i not in found]}


def _hierarchy(ctx: Ctx, args: argparse.Namespace, direction: str) -> Dict[str, Any]:
    """Walk CONCEPT_ANCESTOR from the input IDs down (descendants) or up (ancestors).

    Each returned concept carries MIN(min_levels_of_separation) and
    MAX(max_levels_of_separation) over its pairs with the input IDs, so 1 means a
    direct child / parent of at least one input concept. The self-pair (level 0) is
    excluded. `--max-levels N` keeps pairs with min_levels_of_separation <= N.
    """
    ids = list(dict.fromkeys(args.concept_ids))
    placeholders, params = _in_list(ids)
    given, other = (
        ("ancestor", "descendant") if direction == "descendants" else ("descendant", "ancestor")
    )
    where = [
        f"a.{given}_concept_id IN {placeholders}",
        "a.descendant_concept_id <> a.ancestor_concept_id",
    ]
    if args.max_levels is not None:
        if args.max_levels < 1:
            raise ToolError("--max-levels must be >= 1")
        where.append("a.min_levels_of_separation <= ?")
        params.append(args.max_levels)
    if args.domain_id:
        where.append("UPPER(c.domain_id) = UPPER(?)")
        params.append(args.domain_id)
    if args.vocab_list:
        vsql, vparams = _vocab_filter(args.vocab_list)
        where.append(vsql)
        params += vparams
    where.append(ctx.patient_clause("c"))
    cols = ", ".join("c." + col.strip() for col in CONCEPT_COLS.split(","))
    rows = ctx.db.query(
        f"SELECT {cols}, MIN(a.min_levels_of_separation) AS min_levels_of_separation, "
        "MAX(a.max_levels_of_separation) AS max_levels_of_separation "
        f"FROM {ctx.db.vocab('concept_ancestor')} a "
        f"JOIN {ctx.db.vocab('concept')} c ON c.concept_id = a.{other}_concept_id "
        f"WHERE {' AND '.join(where)} GROUP BY {cols} ORDER BY c.concept_id",
        params,
    )
    noun = direction[:-1]  # descendant / ancestor
    out = {
        **ctx.meta(),
        "concept_ids": ids,
        "domain_id": args.domain_id,
        "max_levels": args.max_levels,
        f"{noun}_count": len(rows),
        direction: [int(r["concept_id"]) for r in rows],
    }
    if args.with_metadata:
        out["items"] = [
            {
                **_concept_to_dict(r),
                "min_levels_of_separation": int(r["min_levels_of_separation"]),
                "max_levels_of_separation": int(r["max_levels_of_separation"]),
            }
            for r in rows
        ]
    return out


def cmd_get_descendants(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    return _hierarchy(ctx, args, "descendants")


def cmd_get_ancestors(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    return _hierarchy(ctx, args, "ancestors")


def cmd_get_mappings(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    ids = list(dict.fromkeys(args.concept_ids))
    rel_ids = args.relationship_ids or [
        "Maps to",
        "Mapped from",
        "Maps to value",
        "Mapped from value",
    ]
    id_ph, id_params = _in_list(ids)
    rel_ph, rel_params = _in_list(rel_ids)
    rel = ctx.db.vocab("concept_relationship")
    mapped = (
        f"SELECT concept_id_2 AS mapped_id, concept_id_1 AS source_id, relationship_id FROM {rel} "
        f"WHERE concept_id_1 IN {id_ph} AND relationship_id IN {rel_ph} "
        f"UNION SELECT concept_id_1, concept_id_2, relationship_id FROM {rel} "
        f"WHERE concept_id_2 IN {id_ph} AND relationship_id IN {rel_ph}"
    )
    params = id_params + rel_params + id_params + rel_params + [args.target_vocabulary]
    rows = ctx.db.query(
        f"SELECT DISTINCT m.mapped_id, m.source_id, m.relationship_id FROM ({mapped}) m "
        f"JOIN {ctx.db.vocab('concept')} c ON c.concept_id = m.mapped_id "
        f"WHERE UPPER(c.vocabulary_id) = UPPER(?) AND {ctx.patient_clause('c')} "
        "ORDER BY m.mapped_id",
        params,
    )
    return {
        **ctx.meta(),
        "concept_ids": ids,
        "target_vocabulary": args.target_vocabulary,
        "mapped_concept_ids": sorted({int(r["mapped_id"]) for r in rows}),
        "pairs": [
            {
                "source_id": int(r["source_id"]),
                "mapped_id": int(r["mapped_id"]),
                "relationship_id": r["relationship_id"],
            }
            for r in rows
        ],
    }


def cmd_count_patients(ctx: Ctx, args: argparse.Namespace) -> Dict[str, Any]:
    domain = args.domain_id.strip().upper()
    if domain not in ctx.domain_map:
        raise ToolError(f"Unknown domain_id: {domain} (known: {', '.join(ctx.domain_map)})")
    table, columns = ctx.domain_map[domain]
    available = [c for c in columns if c in ctx.db.cdm_columns(table)]
    if args.column == "standard":
        available = [c for c in available if "_source_" not in c]
    elif args.column == "source":
        available = [c for c in available if "_source_" in c]
    if not available:
        raise ToolError(
            f"CDM table '{table}' is not linked (or lacks {args.column} concept columns)"
        )
    if "person_id" not in ctx.db.cdm_columns(table):
        raise ToolError(f"CDM table '{table}' has no person_id column")
    ids = list(dict.fromkeys(args.concept_ids))
    id_ph, id_params = _in_list(ids)
    id_set = f"SELECT concept_id FROM {ctx.db.vocab('concept')} WHERE concept_id IN {id_ph}"
    params: List[Any] = list(id_params)
    if args.include_descendants:
        id_set += (
            f" UNION SELECT descendant_concept_id FROM {ctx.db.vocab('concept_ancestor')} "
            f"WHERE ancestor_concept_id IN {id_ph}"
        )
        params += id_params
    ref = ctx.db.cdm(table)
    cond = " OR ".join(f"t.{c} IN (SELECT concept_id FROM ids)" for c in available)
    row = ctx.db.query(
        f"WITH ids AS ({id_set}) SELECT COUNT(DISTINCT t.person_id) AS n, "
        f"(SELECT COUNT(*) FROM ids) AS expanded FROM {ref} t WHERE {cond}",
        params,
    )[0]
    return {
        "domain_id": domain,
        "table": table,
        "columns": available,
        "concept_id_count": len(ids),
        "expanded_concept_id_count": int(row["expanded"] or 0),
        "include_descendants": args.include_descendants,
        "count": int(row["n"] or 0),
    }


# ---------------------------------------------------------------------------
# Argument parser & dispatch
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vocab_tool", description="OMOP vocabulary query CLI (DuckDB / Postgres)"
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"config.yaml (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--patient-filter",
        choices=["config", "on", "off"],
        default="config",
        help="Override only_with_patient for this call (default: use config)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "check",
        help="Check the config and connection: backend, tables found, patient filter state and "
        "linked CDM domains. Returns configured=false with setup steps when there is no config.",
    )
    sub.add_parser("build-cache", help="(duckdb) (Re)build the materialized cache_db")
    sub.add_parser("list-vocabularies", help="List vocabulary_ids with concept counts")

    p = sub.add_parser("list-vocabularies-by-table", help="Vocabularies present in a CDM table")
    p.add_argument("--table", required=True, help="CDM table, e.g. condition_occurrence")

    p = sub.add_parser("list-domains-by-vocabulary", help="Domains a vocabulary's concepts lie in")
    p.add_argument("--vocabulary", required=True)

    p = sub.add_parser(
        "search-concepts",
        help="Search concept names and synonyms; exact matches are ranked first",
    )
    p.add_argument("--query", required=True, help="Term or phrase")
    p.add_argument("--vocab-list", nargs="+", help="vocabulary_ids, e.g. SNOMED ICD10CM")
    p.add_argument("--domain-id", help="Optional domain filter, e.g. Condition, Drug")
    p.add_argument("--top-k", type=int, default=50, help="Max results (floor 50)")
    p.add_argument("--fuzzy", action="store_true", help="Match all tokens in any order")
    p.add_argument("--no-synonyms", action="store_true", help="Search concept names only")
    p.add_argument("--standard-only", action="store_true", help="Standard concepts only")
    p.add_argument(
        "--include-invalid", action="store_true", help="Include deprecated/updated concepts"
    )
    p.add_argument("--use-synonyms", action="store_true", help=argparse.SUPPRESS)  # legacy no-op

    p = sub.add_parser("lookup-concept-by-code", help="Exact lookup of one source code")
    p.add_argument("--vocabulary", required=True, help="vocabulary_id, e.g. ICD10CM")
    p.add_argument("--code", required=True, help="Code as a string, e.g. E11.9 or 331.82")

    p = sub.add_parser(
        "search-concepts-by-code-pattern", help="Codes matching a SQL LIKE pattern, e.g. 'E11%%'"
    )
    p.add_argument("--vocabulary", required=True)
    p.add_argument("--pattern", required=True, help="LIKE pattern, e.g. E11%%")
    p.add_argument("--top-k", type=int, default=100, help="Max results (floor 50)")

    p = sub.add_parser(
        "validate-codes", help="Which codes exist in a vocabulary, with prefix suggestions"
    )
    p.add_argument("--vocabulary", required=True)
    p.add_argument("--codes", nargs="+", required=True)

    p = sub.add_parser(
        "validate-concept-ids", help="Which concept_ids exist (never patient-filtered)"
    )
    p.add_argument("--concept-ids", type=int, nargs="+", required=True)

    p = sub.add_parser("codes-to-concept-ids", help="Source codes to their concept_ids")
    p.add_argument("--vocabulary", required=True)
    p.add_argument("--codes", nargs="+", required=True)

    p = sub.add_parser(
        "fetch-concept-metadata",
        help="Names, codes, domains and validity of concept_ids, plus has_patient_data when "
        "patient data is connected (never patient-filtered). Validate final codesets with it.",
    )
    p.add_argument("--concept-ids", type=int, nargs="+", required=True)
    p.add_argument("--include-synonyms", action="store_true")

    for name, word, direct in (
        ("get-descendants", "descendants", "children"),
        ("get-ancestors", "ancestors", "parents"),
    ):
        p = sub.add_parser(name, help=f"{word.capitalize()} via CONCEPT_ANCESTOR")
        p.add_argument("--concept-ids", type=int, nargs="+", required=True)
        p.add_argument("--domain-id")
        p.add_argument("--vocab-list", nargs="+")
        p.add_argument(
            "--max-levels",
            type=int,
            help=f"Keep {word} at most N levels away (1 = direct {direct})",
        )
        p.add_argument(
            "--with-metadata",
            action="store_true",
            help="Full concept rows with min/max levels of separation",
        )

    p = sub.add_parser(
        "get-mappings", help="Follow 'Maps to' / 'Mapped from' into a target vocabulary"
    )
    p.add_argument("--concept-ids", type=int, nargs="+", required=True)
    p.add_argument("--target-vocabulary", required=True)
    p.add_argument(
        "--relationship-ids",
        nargs="+",
        help="Default: Maps to, Mapped from, Maps to value, Mapped from value",
    )

    p = sub.add_parser(
        "count-patients",
        help="Distinct persons with records of the concepts in the CDM table of a domain "
        "(needs connected patient data)",
    )
    p.add_argument("--domain-id", required=True)
    p.add_argument("--concept-ids", type=int, nargs="+", required=True)
    p.add_argument("--include-descendants", action="store_true")
    p.add_argument(
        "--column",
        choices=["both", "standard", "source"],
        default="both",
        help="Which concept column(s) of the table to match",
    )
    return parser


HANDLERS = {
    "check": cmd_check,
    "build-cache": cmd_build_cache,
    "list-vocabularies": cmd_list_vocabularies,
    "list-vocabularies-by-table": cmd_list_vocabularies_by_table,
    "list-domains-by-vocabulary": cmd_list_domains_by_vocabulary,
    "search-concepts": cmd_search_concepts,
    "lookup-concept-by-code": cmd_lookup_concept_by_code,
    "search-concepts-by-code-pattern": cmd_search_concepts_by_code_pattern,
    "validate-codes": cmd_validate_codes,
    "validate-concept-ids": cmd_validate_concept_ids,
    "codes-to-concept-ids": cmd_codes_to_concept_ids,
    "fetch-concept-metadata": cmd_fetch_concept_metadata,
    "get-descendants": cmd_get_descendants,
    "get-ancestors": cmd_get_ancestors,
    "get-mappings": cmd_get_mappings,
    "count-patients": cmd_count_patients,
}


def _hoist_global_flags(argv: List[str]) -> List[str]:
    """Allow --config / --patient-filter anywhere on the command line."""
    front: List[str] = []
    rest: List[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--config", "--patient-filter") and i + 1 < len(argv):
            front += [arg, argv[i + 1]]
            i += 2
            continue
        if arg.startswith("--config=") or arg.startswith("--patient-filter="):
            front.append(arg)
        else:
            rest.append(arg)
        i += 1
    return front + rest


def run(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    args = build_parser().parse_args(
        _hoist_global_flags(list(sys.argv[1:] if argv is None else argv))
    )
    config_path = resolve_config_path(args.config)
    if args.command == "check" and not config_path.is_file():
        return {"ok": False, "configured": False, "config": str(config_path), "setup": SETUP_HINT}
    cfg = load_config(config_path)
    if args.patient_filter == "config":
        patient_filter = bool(cfg.get("only_with_patient", False))
    else:
        patient_filter = args.patient_filter == "on"
    backend = make_backend(cfg)
    return HANDLERS[args.command](Ctx(cfg, backend, patient_filter), args)


def main(argv: Optional[List[str]] = None) -> None:
    try:
        result = run(argv)
        _output(result)
        if result.get("ok") is False:  # `check` without a config
            sys.exit(1)
    except ToolError as exc:
        _output({"error": str(exc)})
        sys.exit(1)
    except Exception as exc:  # surface DB/IO errors as JSON for the agent
        _output({"error": f"{type(exc).__name__}: {exc}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
