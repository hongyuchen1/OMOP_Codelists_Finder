# OMOP Codelists Finder

An agent skill plus MCP servers for **Claude Code** and **Codex** that build OMOP CDM codesets.
Give it a clinical concept, such as "type 2 diabetes" or "SGLT2 inhibitors", plus the
vocabularies you want (SNOMED, ICD10CM, RxNorm, LOINC, ...). The agent searches, expands and
validates OMOP `concept_id`s against **your** vocabulary and returns an ordered, tool-verified
codeset.

- **Your vocabulary, two ways**: the Athena download folder (CSV/Parquet, queried in place with
  DuckDB, no database needed) or an existing OMOP Postgres database.
- **Optional patient data**: connect the CDM event tables to count patients per concept and,
  if you want, keep only concepts that actually occur in your data (`only_with_patient`).
- **Guided setup**: on first use the agent asks where your vocabulary is and whether to connect
  patient data, then writes the config for you. Passwords never go through the chat.
- **Four MCP servers**: `postgres` (OMOP vocabulary, DuckDB or Postgres), `rxclass` (RxNav drug
  classes), `loinc` (LOINC Search API) and `do` (Disease Ontology). The skill calls them by
  these names and needs all four.

```
.claude-plugin/                 Claude Code plugin + marketplace manifests
skills/omop-codeset-finder/     the skill
├── SKILL.md                    instructions the agent reads
└── references/                 domain strategies (condition / drug / lab / general) and
                                mcp_servers.json, the four servers' commands
server/                         the MCP servers
├── mcp_server.py               entry point: mcp_server.py postgres|rxclass|loinc|do
├── vocab_tool.py               OMOP vocabulary queries (DuckDB or Postgres)
├── configure.py                writes ~/.config/omop-codeset-finder/config.yaml
├── rxclass_tool.py             RxNav drug classes (network)
├── do_tool.py                  Disease Ontology (network)
├── loinc_tool.py               LOINC Search API (network, free LOINC account)
└── config.example.yaml         every configuration option, documented
install.sh                      installs without the plugin (Codex, or Claude Code by hand)
Supplement_Error_Examples.xlsx  supplementary data for the OMOP Codelists Finder paper
```

## 1. Prerequisites

- [**uv**](https://docs.astral.sh/uv/) (Python ≥ 3.10 is fetched by uv if needed). The servers
  declare their dependencies inline, so uv installs them on first start.
- A vocabulary source:
  - **Athena files**: download from [athena.ohdsi.org](https://athena.ohdsi.org) and unzip.
    `CONCEPT.csv` is required; `CONCEPT_SYNONYM.csv`, `CONCEPT_ANCESTOR.csv` and
    `CONCEPT_RELATIONSHIP.csv` enable synonym search, descendants, ancestors and mappings.
  - **or an OMOP Postgres database** with those tables in a vocabulary schema.
- Optional: patient data, as CDM tables in CSV/Parquet files (DuckDB) or a CDM schema (Postgres).

## 2. Install

The skill only works with its MCP servers; every install below sets up both.

### Claude Code (plugin, recommended)

In Claude Code:

```
/plugin marketplace add hongyuchen1/OMOP_Codelists_Finder
/plugin install omop-codeset-finder@omop-codelists-finder
```

or from a shell: `claude plugin marketplace add hongyuchen1/OMOP_Codelists_Finder && claude
plugin install omop-codeset-finder@omop-codelists-finder`. Run `/reload-plugins` (or restart).
This installs the skill and the four MCP servers together; `/mcp` should show `postgres`,
`rxclass`, `loinc` and `do` connected.

### Codex

```bash
git clone https://github.com/hongyuchen1/OMOP_Codelists_Finder.git
cd OMOP_Codelists_Finder
./install.sh --codex        # links the skill into ~/.agents/skills, runs `codex mcp add` x4
```

If `codex` isn't on your PATH, `install.sh` prints the commands. The equivalent
`~/.codex/config.toml` entries are:

```toml
[mcp_servers.postgres]
command = "uv"
args = ["run", "--quiet", "--script", "/path/to/OMOP_Codelists_Finder/server/mcp_server.py", "postgres"]

[mcp_servers.rxclass]
command = "uv"
args = ["run", "--quiet", "--script", "/path/to/OMOP_Codelists_Finder/server/mcp_server.py", "rxclass"]

[mcp_servers.loinc]
command = "uv"
args = ["run", "--quiet", "--script", "/path/to/OMOP_Codelists_Finder/server/mcp_server.py", "loinc"]

[mcp_servers.do]
command = "uv"
args = ["run", "--quiet", "--script", "/path/to/OMOP_Codelists_Finder/server/mcp_server.py", "do"]
```

### Other options

| Goal | Command |
|---|---|
| Claude Code without the plugin | `./install.sh --claude` (skill in `~/.claude/skills`, `claude mcp add --scope user` x4) |
| Both agents | `./install.sh --both` |
| Skill in one repository only | `./install.sh --both --project /path/to/repo` |
| Any other MCP client | four stdio servers: `uv run --quiet --script <repo>/server/mcp_server.py <postgres\|rxclass\|loinc\|do>` |

`install.sh` symlinks the skill, so `git pull` updates it (add `--copy` to copy instead); the
servers run from the checkout. It registers the servers at user scope under their own names,
and installs nothing if one of those names already belongs to another server (for example
your own `postgres` server): remove or rename that server first, or use the plugin, whose
server names are kept apart from yours. Use the plugin **or** `install.sh --claude` for Claude
Code, not both.

## 3. Configure

Open Claude Code or Codex and say **"set up the OMOP codeset finder"**. The agent calls
`check`, asks you the questions below, and calls `configure`, which writes
`~/.config/omop-codeset-finder/config.yaml` (outside the plugin, so updates keep it; set
`$OMOP_CODESET_FINDER_CONFIG` to use another file) and checks the connection. To configure by
hand instead, copy `server/config.example.yaml` there and edit it.

### The three questions

| Question | Answer → `configure` arguments |
|---|---|
| Where is the vocabulary? | Athena folder → `backend: duckdb`, `vocab_dir` · OMOP database → `backend: postgres`, `host`, `port`, `dbname`, `user`, `vocab_schema` (and `sslmode` for TLS) |
| Connect patient data? | No → vocabulary only · Yes → `cdm_dir` (CSV/Parquet folder) or `cdm_schema` (Postgres) |
| Only concepts that occur in your patient data? | `only_with_patient: yes` (filter on by default) or `no` (search the whole vocabulary; you can still count patients) |

The agent replaces an existing config only after you agree.

**Passwords.** The agent never asks for the database password, and `configure` doesn't accept
one. Put it in `~/.config/omop-codeset-finder/.env` (next to the config; `chmod 600` it):

```dotenv
OMOP_DB_PASSWORD=...
```

or rely on `PGPASSWORD` / `~/.pgpass`. It is never written to `config.yaml`. Any config value
may reference environment variables: `${VAR}` or `${VAR:-default}`.

**DuckDB details.**
- Athena exports are tab-delimited and unquoted (the default). For other files set
  `vocab_delimiter: auto`. CDM files are auto-detected.
- Files are found by table name, case-insensitively (`CONCEPT.csv`, `concept.parquet`,
  `condition_occurrence.csv`, ...). For other names, use `vocab_files` / `cdm_files` in the config.
- The vocabulary is materialized once into a cache (`~/.cache/omop-codeset-finder/omop.duckdb`).
  The first `check` on a full Athena download takes a minute or two; later calls take
  milliseconds. The cache rebuilds when a source file changes. CDM tables are never copied.

### What `only_with_patient` does

| Setting | Behaviour |
|---|---|
| `false` | Searches the whole vocabulary. |
| `true` | Discovery tools (search, code lookups, descendants, ancestors, mappings) keep only concepts that appear in the CDM table of their domain. |

- A concept has patients if its ID appears in **either** the standard column (e.g.
  `condition_concept_id`) **or** the source column (`condition_source_concept_id`), so ICD codes
  recorded as source concepts are kept. Change the tables/columns per domain under
  `patient_filter.domains` in the config.
- Concepts from a domain whose CDM table isn't connected pass through unfiltered.
- `fetch_concept_metadata` and `validate_concept_ids` are never filtered; with patient data
  connected, `fetch_concept_metadata` reports `has_patient_data` per concept.
- Any call can override the setting with `patient_filter: on|off`.

With patient data connected the tools return concept-level information and **aggregate**
distinct-person counts only, never patient rows. Those counts reach your agent's model
provider; check your data use agreement if small counts are a concern.

### Optional: LOINC

The `loinc` server uses the LOINC Search API, which needs a free account from
[loinc.org](https://loinc.org). Add to `~/.config/omop-codeset-finder/.env`:

```dotenv
LOINC_USERNAME=you@example.org
LOINC_PASSWORD=...
```

## 4. Use

In Claude Code, ask naturally or invoke the skill:

```
/omop-codeset-finder:omop-codeset-finder type 2 diabetes in SNOMED and ICD10CM
```

In Codex, mention `$omop-codeset-finder` or pick it from `/skills`:

```
$omop-codeset-finder build a codeset for SGLT2 inhibitors in RxNorm
```

The agent picks a domain strategy from `references/`, calls the tools, validates every
candidate, and returns an ordered codeset per vocabulary with notes on gaps.

### The servers and their tools

| Server | Tools |
|---|---|
| `postgres` | `check`, `configure`, `search_concepts`, `lookup_concept_by_code`, `search_concepts_by_code_pattern`, `codes_to_concept_ids`, `validate_codes`, `get_descendants`, `get_ancestors`, `get_mappings`, `fetch_concept_metadata`, `validate_concept_ids`, `count_patients`, `list_vocabularies`, `list_domains_by_vocabulary`, `list_vocabularies_by_table` |
| `rxclass` | `find_class_by_name`, `find_classes_by_id`, `get_class_members`, `get_class_tree`, `get_class_by_drug_name`, `get_class_by_rxcui`, and the other RxClass API endpoints (17 tools) |
| `loinc` | `search_loinc_codes`, `get_loinc_details`, `get_loinc_panels`, `get_loinc_hierarchy`, `search_loinc_parts`, `search_loinc_answer_lists`, `search_loinc_groups` |
| `do` | `search_diseases`, `get_disease_by_id`, `get_disease_by_label`, `get_disease_hierarchy`, `get_xref_mappings`, `list_diseases`, `list_imports`, `get_import_terms` |

The `postgres` server serves both backends; the name is the one the skill uses. Every tool
describes its arguments to the agent.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check` says `"configured": false` | Ask the agent to set the finder up (section 3). |
| `/mcp` shows a server failed right after installing | uv was still downloading dependencies on first start; reconnect from `/mcp`. Make sure `uv` is on the PATH Claude Code / Codex sees. |
| The agent has the skill but no `postgres`/`rxclass`/`loinc`/`do` tools | The MCP servers aren't registered; reinstall (section 2). |
| `install.sh` says a server name already belongs to another server | Remove or rename that server, or install the plugin instead. |
| `CONCEPT file not found` | `vocab_dir` must be the folder that contains `CONCEPT.csv`. |
| `only_with_patient is enabled but no CDM event tables are linked` | Ask the agent to connect patient data (`cdm_dir` / `cdm_schema`) or to set `only_with_patient` to `no`. |
| `Postgres connection failed` | Check host/port/schema with `check`; put the password in `.env` next to the config (or `~/.pgpass`); set `sslmode: require` if the server needs TLS. |
| CSV parse errors on non-Athena files | Set `vocab_delimiter: auto` (or the right delimiter). |
| The first `check` is slow | It builds the DuckDB cache once; later calls are fast. |
| The agent doesn't see the skill | Plugin: `/plugin` → Installed. `install.sh`: check the symlink location, then restart the agent. |

## Supplementary data

`Supplement_Error_Examples.xlsx` holds the supplementary error examples for the OMOP Codelists
Finder paper.

## Maintainers

`claude plugin validate .` checks the plugin and marketplace manifests, and
`claude --plugin-dir .` loads the plugin from a checkout. Bump `version` in
`.claude-plugin/plugin.json` for each release: installed plugins update only when it changes.
