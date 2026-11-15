---
ticket: 1
stage: spec
requirements: [REQ-3]
---
# Limit a Day Plan to 10 Stops

## Summary
A Day Plan must hold at most 10 Stops. When a Traveller attempts to add an 11th Stop to a Day Plan that already contains 10, the operation fails with a `tooManyStops` error (a case of a typed Error enum) and the Day Plan remains unchanged.

## Acceptance criteria
1. Given a Day Plan already has 10 Stops, When a Traveller adds another Stop, Then adding fails with a `tooManyStops` error and the Day Plan is unchanged.
2. Given a Day Plan has fewer than 10 Stops, When a Traveller adds a Stop, Then the Stop is appended to the end of the Day Plan.

## Out of scope
- Removing, reordering or editing Stops.
- Any change to how Stops are displayed.
