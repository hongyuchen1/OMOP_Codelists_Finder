# Domain: Condition (Disease Ontology + Postgres + ICD Hierarchy)

Use this when the concept is a disease/diagnosis or targets SNOMED / ICD10CM.

## Strategy A: DO semantic expansion

- Use Disease Ontology (DO_) `search_diseases` with `names=[...]` and/or `synonyms=[...]` to find candidate DOIDs.
- For each candidate DOID:
  - Pull synonyms and definition via `get_disease_by_id`.
  - Traverse parents/children via `get_disease_hierarchy` to discover related terms.
- Convert discovered labels/synonyms into expanded keyword queries.
- Use Postgres `search_concepts` with `domain_id="CONDITION"` and `vocab_list` (SNOMED, ICD10CM).

## Strategy B: DO xref bridge

- For a selected DOID, call:
  - `get_xref_mappings(doid, "ICD10CM")`
  - `get_xref_mappings(doid, "SNOMEDCT")`
- Resolve codes to OMOP concept_ids with Postgres `codes_to_concept_ids`.

## Strategy C: Postgres direct search

- Use Postgres `search_concepts` (exact + fuzzy) over:
  - original keywords
  - DO-expanded keywords
- Apply `vocab_list` filters and keep recall high.

## Strategy D: ICD hierarchy navigation

- If an ICD10CM prefix is relevant (e.g., `E11`), use Postgres:
  - `search_concepts_by_code_pattern("ICD10CM", "E11%")`
- Use this to enumerate closely related code children before validation.

## Keyword expansion hints

- Qualifiers: acute/chronic, type/subtype, etiology, anatomical site, severity.
- Consider comorbidity framing and common synonyms.

## Multi-domain trigger

- If the condition implies lab monitoring (e.g., diabetes → A1c/glucose), also load `domain_lab.md`.
