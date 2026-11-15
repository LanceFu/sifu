---
id: {{id}}
intent: intent.md
intent_hash: {{intent_hash}}
title: {{title_quoted}}
author: sifu (spec writer)
date: {{date}}
repo: {{repo}}
requirements: [{{requirements}}]
---

# Spec: {{title}}

{#
  Sifu fills this file from the spec writer's output. The code supplies:
    id, intent_hash (first 12 characters of the SHA-256 of intent.md), title, title_quoted, date,
    repo, requirements, summary, traceability_rows, design_items, file_rows, test_rows,
    assumption_items, review_items, out_of_scope_items.
  The *_rows placeholders expand to table rows and the *_items placeholders to bullet lines, so
  keep each on a line of its own under its table header or heading. Edit the headings, tables'
  header rows, fixed sentences and front matter freely.
  Never write a protected path (router.protected_paths, for example the CI workflow folder) in
  the fixed text: the router reads this file, and a mention forces a human decision on every ticket.
#}
## 1. Summary

{{summary}} Implements the intent in {{id}}; no behaviour is added beyond it.

## 2. Traceability

| Intent item | Satisfied by | Verified by |
| :---- | :---- | :---- |
{{traceability_rows}}

## 3. Design

{{design_items}}

## 4. Files touched

| File | Protected path? |
| :---- | :---- |
{{file_rows}}

A protected path forces a human decision in the risk router.

## 5. Acceptance criteria and test plan

| ID | AC | Case |
| :---- | :---- | :---- |
{{test_rows}}

Every row needs an automated test. Build, SwiftLint and tests must pass.

## 6. Assumptions

{{assumption_items}}

## 7. Review notes

{{review_items}}

## 8. Out of scope

{{out_of_scope_items}}
