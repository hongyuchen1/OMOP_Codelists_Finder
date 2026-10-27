# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.20,<2", "duckdb>=1.1", "pyyaml>=6.0", "psycopg[binary]>=3.1", "requests>=2.31",
# ]
# ///
"""mcp_server.py — the codeset finder's MCP servers (stdio), one per tool module.

    mcp_server.py postgres   OMOP vocabulary (vocab_tool.py, DuckDB or Postgres) + configure
    mcp_server.py rxclass    RxNav drug classes (rxclass_tool.py)
    mcp_server.py loinc      LOINC Search API (loinc_tool.py)
    mcp_server.py do         Disease Ontology (do_tool.py)

Every subcommand of the module is one MCP tool with the same arguments, built from the
module's own argparse parser, so the tools and the parsers cannot drift apart:

    vocab_tool.py search-concepts --vocab-list SNOMED   ->  search_concepts(vocab_list=[...])
    rxclass_tool.py get-class-members --class-id X       ->  get_class_members(class_id="X")
    configure.py --backend duckdb ...                    ->  configure(backend="duckdb", ...)

File locations and secrets (--config, --output, --password) are not exposed. The config is
~/.config/omop-codeset-finder/config.yaml (or $OMOP_CODESET_FINDER_CONFIG), read on every
call, so `configure` takes effect at once.

Run:  uv run --script server/mcp_server.py postgres
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anyio  # noqa: E402
import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

import configure  # noqa: E402
import do_tool  # noqa: E402
import loinc_tool  # noqa: E402
import rxclass_tool  # noqa: E402
import vocab_tool  # noqa: E402

HIDDEN = {"help", "config", "output", "password", "no_check"}
SKIPPED = {"build-cache"}  # `check` builds the DuckDB cache when it is needed

# server name -> (module, label for tool descriptions, server instructions)
SERVERS: Dict[str, tuple[ModuleType, str, str]] = {
    "postgres": (
        vocab_tool,
        "OMOP vocabulary",
        "OMOP vocabulary tools: Athena vocabulary files (backend duckdb) or an OMOP Postgres "
        "database (backend postgres). Call `check` first. If it returns configured=false, set "
        "the finder up with the user before anything else: ask the questions in its `setup` "
        "field (vocabulary source and connection details; whether to connect patient data; if "
        "so, whether to return only concepts that occur in it), never guessing paths, hosts or "
        "schemas, then call `configure` with the answers. Pass overwrite only after the user "
        "agrees to replace an existing config. Passwords never go through the chat or tool "
        "arguments: the user puts OMOP_DB_PASSWORD=... in the .env file next to the config, or "
        "uses ~/.pgpass or PGPASSWORD; then call `check` again. When `check` reports "
        "only_with_patient=true, discovery tools return only concepts that occur in the "
        "patient data; patient_filter='off' searches the whole vocabulary for one call. Never "
        "invent concept_ids; validate final codesets with fetch_concept_metadata. Codes are "
        "strings ('331.82', 'E11.9').",
    ),
    "rxclass": (
        rxclass_tool,
        "RxClass",
        "RxNav RxClass drug-class tools (public web API, no setup). Class members come back "
        "as RxNorm RXCUIs; resolve them to OMOP concept_ids with the postgres server.",
    ),
    "loinc": (
        loinc_tool,
        "LOINC",
        "LOINC Search API tools. They need a free loinc.org account: the user puts "
        "LOINC_USERNAME=... and LOINC_PASSWORD=... in the .env file next to the finder's "
        "config (never in the chat). Resolve LOINC codes to OMOP concept_ids with the postgres "
        "server.",
    ),
    "do": (
        do_tool,
        "Disease Ontology",
        "Disease Ontology tools (public web API, no setup): disease synonyms, hierarchy and "
        "ICD10CM/SNOMEDCT cross-references. Resolve codes to OMOP concept_ids with the "
        "postgres server.",
    ),
}
CONFIGURE_DESCRIPTION = (
    "Write the finder's config and check it. Ask the user first, never guessing values: the "
    "vocabulary source (backend duckdb + vocab_dir, or backend postgres + host, port, dbname, "
    "user, vocab_schema, sslmode), whether to connect patient data (cdm_dir or cdm_schema), "
    "and if so only_with_patient yes|no. Passwords are not accepted here: the user puts "
    "OMOP_DB_PASSWORD in the .env file next to the config."
)


@dataclass
class Command:
    module: ModuleType
    command: List[str]  # the subcommand, if any
    flags: Dict[str, argparse.Action]  # property name -> argparse action
    top: Set[str]  # properties that are flags of the main parser (they go before `command`)
    tool: types.Tool


def _text(help_text: str | None, values: Dict[str, Any] | None = None) -> str:
    """Help text as argparse prints it ("%%" -> "%", "%(default)s" filled in)."""
    return (help_text or "") % (values or {})


def _property(action: argparse.Action) -> Dict[str, Any]:
    if isinstance(action, argparse._StoreTrueAction):
        prop: Dict[str, Any] = {"type": "boolean"}
    else:
        kind = {int: "integer", float: "number"}.get(action.type, "string")
        item: Dict[str, Any] = {"type": kind}
        if action.choices:
            item["enum"] = list(action.choices)
        prop = {"type": "array", "items": item} if action.nargs in ("+", "*") else item
        if action.default not in (None, argparse.SUPPRESS):
            prop["default"] = action.default
    if action.help:
        prop["description"] = _text(action.help, vars(action))
    return prop


def _command(
    module: ModuleType,
    name: str,
    command: List[str],
    description: str,
    top: List[argparse.Action],
    actions: List[argparse.Action],
) -> Command:
    flags: Dict[str, argparse.Action] = {}
    props: Dict[str, Any] = {}
    required: List[str] = []
    for action in top + actions:
        if action.dest in HIDDEN or action.help == argparse.SUPPRESS:
            continue
        if not action.option_strings:
            raise RuntimeError(f"{name}: positional argument {action.dest} is not supported")
        flags[action.dest] = action
        props[action.dest] = _property(action)
        if action.required:
            required.append(action.dest)
    schema = {"type": "object", "properties": props, "required": required}
    tool = types.Tool(name=name, description=description, inputSchema=schema)
    return Command(module, command, flags, {a.dest for a in top}, tool)


def build_commands(server: str) -> Dict[str, Command]:
    module, label, _ = SERVERS[server]
    parser = module.build_parser()
    [subparsers] = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    top = [a for a in parser._actions if a is not subparsers]
    helps = {a.dest: a.help for a in subparsers._choices_actions}
    commands: Dict[str, Command] = {}
    for sub, subparser in subparsers.choices.items():
        if sub in SKIPPED:
            continue
        name = sub.replace("-", "_")
        description = f"[{label}] {_text(helps.get(sub) or sub)}"
        commands[name] = _command(module, name, [sub], description, top, subparser._actions)
    if server == "postgres":
        commands["configure"] = _command(
            configure, "configure", [], CONFIGURE_DESCRIPTION, [], configure.build_parser()._actions
        )
    return commands


def to_argv(cmd: Command, arguments: Dict[str, Any]) -> List[str]:
    front: List[str] = []
    back: List[str] = []
    for dest, value in arguments.items():
        action = cmd.flags.get(dest)
        if action is None:
            raise ValueError(f"Unknown argument: {dest}")
        argv = front if dest in cmd.top else back
        flag = max(action.option_strings, key=len)
        if isinstance(action, argparse._StoreTrueAction):
            if value:
                argv.append(flag)
        elif value is None:
            continue
        elif action.nargs in ("+", "*"):
            argv += [flag, *(str(v) for v in (value if isinstance(value, list) else [value]))]
        else:
            argv.append(f"{flag}={value}")  # '=' keeps values such as "-1" from reading as flags
    return front + cmd.command + back


def call(commands: Dict[str, Command], name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Run one tool and return its JSON result; failures come back as {"error": ...}."""
    cmd = commands.get(name)
    if cmd is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return cmd.module.run(to_argv(cmd, arguments or {}))
    except SystemExit as exc:  # argparse rejected the arguments (details on stderr)
        return {"error": f"Invalid arguments for {name} (exit {exc.code})"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def serve(name: str) -> None:
    commands = build_commands(name)
    server: Server = Server(f"omop-codeset-finder-{name}", instructions=SERVERS[name][2])
    one_at_a_time = anyio.Lock()  # DuckDB cache builds and config writes must not overlap

    @server.list_tools()
    async def list_tools() -> List[types.Tool]:
        return [c.tool for c in commands.values()]

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
        async with one_at_a_time:
            result = await anyio.to_thread.run_sync(call, commands, name, arguments)
        text = json.dumps(result, default=str, ensure_ascii=False)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=isinstance(result, dict) and "error" in result,
        )

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="mcp_server", description=__doc__.splitlines()[0])
    parser.add_argument("server", choices=list(SERVERS), help="Which MCP server to run")
    serve(parser.parse_args().server)
