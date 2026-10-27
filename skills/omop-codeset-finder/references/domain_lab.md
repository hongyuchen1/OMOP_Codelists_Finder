# Domain: Lab / Measurement (LOINC + Postgres)

Use this when the concept is a lab test/measurement or targets LOINC.

## Strategy A: LOINC semantic expansion

- Use `search_loinc_codes(query, rows=...)` to discover related lab tests.
- Collect LOINC codes + full names; turn names into expanded keyword queries.
- Use Postgres `search_concepts` with `vocab_list=["LOINC"]` and `domain_id="MEASUREMENT"`.

## Strategy B: LOINC panel discovery

- Use `get_loinc_panels(loinc_code)` for panel composition (e.g., BMP → glucose, BUN, creatinine).
- Expand search keywords using member test names/codes.

## Strategy C: LOINC hierarchy exploration

- Use `get_loinc_hierarchy(loinc_code)` to discover parent/child relationships.
- Add discovered related measurement names as Postgres search keywords.

## Strategy D: Postgres direct search

- Use Postgres `search_concepts` over original + LOINC-expanded keywords.
- Keep `domain_id="MEASUREMENT"` unless the intent is Observation.

## Keyword expansion hints

- Analyte + specimen + method; quantitative vs qualitative; panels vs individual tests.

## Multi-domain trigger

- If the lab implies an underlying condition, consider loading `domain_condition.md`.
