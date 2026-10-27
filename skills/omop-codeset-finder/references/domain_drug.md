# Domain: Drug (RxClass + Postgres Descendants)

Use this when the concept is a medication, drug class, ingredient, or targets RxNorm.

## Strategy A: RxClass by name

- Use `find_class_by_name` to locate candidate drug classes.
- Expand members via `get_class_members(class_id)` to enumerate drugs (RXCUIs).

## Strategy B: RxClass by class code

- Use `find_classes_by_id` when you have an identifier/code.
- Use `get_class_tree` to enumerate subclasses/descendants where helpful.

## Strategy C: Postgres ingredient search + descendants

- Use Postgres `search_concepts` with `domain_id="DRUG"` and `vocab_list=["RxNorm"]`.
- For ingredient-level concepts, expand to products/forms via Postgres `get_descendants`.

## Strategy D: Reverse lookup

- Use `get_class_by_drug_name` to identify classes containing a known drug name.
- Then expand with Strategy A/B for class members.

## Fallback chain

- A → B → C → D (repeat with broadened synonyms if any step is empty).

## Keyword expansion hints

- Ingredient ↔ brand ↔ class name; include combinations, salts, routes/forms.
- Use `top_k` ≥ 200 for ingredient searches and class expansions.
