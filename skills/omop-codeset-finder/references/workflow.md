# Workflow (Optional)

Load this only when you need procedural structure or the concept doesn’t fit a dedicated domain file.

## Stage 0: Domain & Table Inference

- Use vocabulary cues (RxNorm→Drug, LOINC→Measurement, ICD10CM/SNOMED→Condition, CPT4→Procedure).
- Use term cues (dose/route→Drug, analyte/specimen→Measurement, diagnosis wording→Condition).

## Stage 1: Keyword Expansion

- Generate synonyms, abbreviations, spelling variants, and common subtypes.
- Add qualifiers that change meaning (acute/chronic, type 1/2, fasting/random, route/form).

## Stage 2: Concept Search

- Use Postgres `search_concepts` with appropriate `vocab_list` and `domain_id`.
- For codes, use `lookup_concept_by_code` and `search_concepts_by_code_pattern` where applicable.
- For drugs, use RxClass to expand classes → then Postgres `get_descendants` for product forms.

## Stage 3: Validation

- Validate every final candidate with `fetch_concept_metadata`.
- Drop obvious mismatches; keep plausible borderline candidates when aiming for recall.

## Stage 4: Iterate

- If results are thin: broaden keywords, try fuzzy search, switch domain filters.
- If results are noisy: add qualifiers/negatives, narrow domain/vocab filters.

## Stage 5: Finalize

- Dedupe within vocabulary while preserving order.
- Provide short gap/limitation notes per vocabulary.
