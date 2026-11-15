---
ticket: 1
stage: spec
requirements: [REQ-3]
---
# Remove a Stop from a Day Plan

## Summary
A Day Plan can remove one Stop by its id and returns the removed Stop. The remaining Stops keep their order. An id that matches no Stop fails with a `stopNotFound` error and the Day Plan is unchanged.

## Acceptance criteria
1. Given a Day Plan with Stops A, B and C, When B is removed, Then the Day Plan lists A then C and the removed Stop is B.
2. Given a Day Plan, When an id that matches no Stop is removed, Then it fails with `stopNotFound` and the Stops are unchanged.
3. Given a Day Plan with one Stop, When that Stop is removed, Then the Day Plan has no Stops.

## Out of scope
- Removing several Stops at once and undo.
