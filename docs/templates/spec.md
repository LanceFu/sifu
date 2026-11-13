---
id: TP-0000
intent: intent.md
intent_hash: [first 12 characters of the SHA-256 of intent.md, set by code]
title: "[Same as the intent title]"
author: sifu (spec writer)
date: YYYY-MM-DD
repo: tripplanner-ios
requirements: [] # REQ-n from the PRD
---

# Spec: [title]

## 1. Summary

[Two or three sentences on what gets built.] Implements the intent in TP-0000; no behaviour is added beyond it.

## 2. Traceability

| Intent item | Satisfied by | Verified by |
| :---- | :---- | :---- |
| AC-1: [short name] | [files, or Section 3] | T1 |
| REQ-n | Section 3 | T1 |

## 3. Design

- [Decision, and why.]

## 4. Files touched

| File | Protected path? |
| :---- | :---- |
| `[path]` | yes / no |

A protected path (for example `.github/`) forces a human decision in the risk router.

## 5. Acceptance criteria and test plan

| ID | AC | Case |
| :---- | :---- | :---- |
| T1 | AC-1 | [the Given / When / Then line] |

Every row needs an automated test. Build, SwiftLint and tests must pass.

## 6. Assumptions

- [Anything the spec writer had to assume because the ticket did not say.]

## 7. Review notes

- [severity] [reviewer]: [message from the reviewers and the deterministic checks]

## 8. Out of scope

- [What this change deliberately will not build or change]
