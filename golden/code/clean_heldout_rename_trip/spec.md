---
ticket: 1
stage: spec
requirements: [REQ-1]
---
# Rename a Trip

## Summary
A Trip can be renamed. A blank new name (empty, or only spaces and line breaks) fails with the `nameBlank` error and the Trip keeps its old name. A valid name is stored exactly as typed. Nothing else about the Trip changes.

## Acceptance criteria
1. Given a Trip named "Rome", When it is renamed to "Paris", Then its name is "Paris".
2. Given a Trip named "Rome", When it is renamed to an empty name, to spaces only or to line breaks only, Then renaming fails with `nameBlank` and the name is still "Rome".
3. Given a new name with spaces around it, When the Trip is renamed, Then the name is stored with those spaces unchanged.

## Out of scope
- Any other change to a Trip, and a maximum name length.
