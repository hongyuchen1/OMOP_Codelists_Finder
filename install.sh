#!/usr/bin/env bash
# Install the omop-codeset-finder skill and its MCP servers without the Claude Code plugin.
#
#   ./install.sh                     # Claude Code, user scope (~/.claude/skills)
#   ./install.sh --codex             # Codex, user scope (~/.agents/skills)
#   ./install.sh --both
#   ./install.sh --both --project /path/to/repo     # skill in <repo>/.claude|.agents/skills
#   ./install.sh --copy              # copy the skill instead of symlinking it
#
# The skill is symlinked by default, so `git pull` here updates every agent. The skill needs
# the four MCP servers postgres, rxclass, loinc and do; they are registered at user scope with
# `claude mcp add` / `codex mcp add` and run from this checkout. If one of those names already
# belongs to another server, nothing is installed. Claude Code users can install the plugin
# instead (see README); don't do both.
set -euo pipefail

SKILL_NAME="omop-codeset-finder"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/skills/$SKILL_NAME"
SERVER="$ROOT/server/mcp_server.py"
SERVERS=(postgres rxclass loinc do)
targets=()
project=""
mode="link"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --claude) targets+=("claude") ;;
    --codex) targets+=("codex") ;;
    --both) targets+=("claude" "codex") ;;
    --project) project="$(cd "$2" && pwd)"; shift ;;
    --copy) mode="copy" ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
  shift
done
[[ ${#targets[@]} -eq 0 ]] && targets=("claude")

# Prints "ours", "free" or "taken" for MCP server <name> in <agent>.
server_state() {
  local agent="$1" name="$2" out
  if ! out="$("$agent" mcp get "$name" 2>/dev/null)"; then
    echo free
  elif grep -qF "$SERVER" <<<"$out"; then
    echo ours
  else
    echo taken
  fi
}

# Refuse before changing anything if a server name belongs to someone else.
clashes=()
for t in "${targets[@]}"; do
  command -v "$t" >/dev/null 2>&1 || continue
  for name in "${SERVERS[@]}"; do
    [[ "$(server_state "$t" "$name")" == "taken" ]] && clashes+=("$t: $name")
  done
done
if [[ ${#clashes[@]} -gt 0 ]]; then
  echo "These MCP server names already belong to other servers:" >&2
  printf '  %s\n' "${clashes[@]}" >&2
  echo "The skill calls its servers by these names. Remove or rename those servers" \
    "(e.g. \`claude mcp remove postgres\`), or install the Claude Code plugin, whose" \
    "server names cannot clash. Nothing was installed." >&2
  exit 1
fi

install_to() {
  local dir="$1" dest="$1/$SKILL_NAME"
  mkdir -p "$dir"
  if [[ -L "$dest" || -e "$dest" ]]; then
    if [[ -L "$dest" && "$(readlink "$dest")" == "$SRC" ]]; then
      echo "Already installed: $dest -> $SRC"; return
    fi
    echo "Refusing to overwrite existing $dest (remove it first)." >&2; return 1
  fi
  if [[ "$mode" == "copy" ]]; then
    cp -R "$SRC" "$dest"
  else
    ln -s "$SRC" "$dest"
  fi
  echo "Installed ($mode): $dest"
}

register_servers() {
  local agent="$1" name
  if ! command -v "$agent" >/dev/null 2>&1; then
    echo "'$agent' not on PATH; register the MCP servers yourself:"
    for name in "${SERVERS[@]}"; do
      echo "  $agent mcp add $name -- uv run --quiet --script \"$SERVER\" $name"
    done
    return
  fi
  for name in "${SERVERS[@]}"; do
    local cmd=(uv run --quiet --script "$SERVER" "$name")
    if [[ "$(server_state "$agent" "$name")" == "ours" ]]; then
      echo "$agent: MCP server '$name' already registered."
    elif [[ "$agent" == "claude" ]]; then
      claude mcp add --scope user "$name" -- "${cmd[@]}"
    else
      codex mcp add "$name" -- "${cmd[@]}"
    fi
  done
}

command -v uv >/dev/null 2>&1 || echo "Note: uv is not on PATH; the MCP servers need it" \
  "(https://docs.astral.sh/uv/)." >&2

for t in "${targets[@]}"; do
  case "$t" in
    claude) dir="${project:-$HOME}/.claude/skills" ;;
    codex) dir="${project:-$HOME}/.agents/skills" ;;
  esac
  install_to "$dir"
  register_servers "$t"
done

echo "Next: restart the agent and ask it to \"set up the OMOP codeset finder\"."
