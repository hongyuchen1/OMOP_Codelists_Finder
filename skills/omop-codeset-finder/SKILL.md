---
name: omop-codeset-finder
description: Retrieve candidate OMOP CDM `concept_id`s related to a user-provided concept across specified vocabularies, using an evidence-first, iterative search-and-validate workflow. Trigger when the user is building an OMOP codeset, mapping a term/code to OMOP concepts, expanding drug classes, or needs concept ID candidates for a selector to finalize.
---
---
# OMOP Codeset Finder (Slim Routing Skill)

Use this skill to retrieve **ordered candidate OMOP `concept_id`s** with evidence.
Domain-specific strategies live in `skills/omop-codeset-finder/references/`.

## Non-Negotiable Rules

1. No invented `concept_id`s (only tool outputs).
2. Validate candidates with Postgres `fetch_concept_metadata` before finalizing.
3. Prefer recall over precision unless the user asks to narrow.
4. Never set Postgres `top_k` below 50 (use ≥200 for ingredient/class expansion).
5. Avoid redundant loops: if results repeat, change query/strategy/tool.

## Inputs

- Concept: term/phrase/code.
- Target vocabularies (e.g., SNOMED, RxNorm, ICD10CM, LOINC).
- Target CDM table (optional; infer if missing).

## Domain Routing (Load Reference Files)

Infer domain(s) from semantics + target vocabularies, then load the matching file(s):

- Condition / diagnosis → `references/domain_condition.md`
- Drug / medication → `references/domain_drug.md`
- Lab / measurement → `references/domain_lab.md`
- Unclear / no domain file fits → `references/workflow.md`

Multi-domain concepts should load multiple reference files (e.g., diabetes → condition + lab).

## MCP Activation Guidance

- Always available: Postgres + RxClass
- Activate LOINC tools for lab concepts; activate Disease Ontology tools for condition concepts
- See `references/mcp_servers.json` for the canonical server config keys

## Output Template

- Inferred domain / CDM table
- Vocabularies used
- Ordered codeset (vocabulary → ordered `concept_id` list)
- Summary: gap signals, rationale, limitations

## What NOT to Do

- Don’t stop at “tools returned nothing”; switch strategy or broaden keywords first.
- Don’t output long narratives; keep outputs tool-grounded and codeset-focused.
