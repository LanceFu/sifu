---
ticket: 1
stage: spec
requirements: [REQ-7]
---
# Warn when a Day Plan runs long

## Summary
A Day Plan reports whether its total Duration exceeds 960 minutes (16 hours). The app uses this to show a warning. The warning never blocks the plan: adding a Stop works exactly as before.

## Acceptance criteria
1. Given a Day Plan whose total Duration is 961 minutes or more, When the flag is read, Then it is true.
2. Given a Day Plan whose total Duration is exactly 960 minutes, When the flag is read, Then it is false.
3. Given a Day Plan at 960 minutes, When a Traveller adds another Stop, Then the Stop is added and nothing is thrown.

## Out of scope
- Showing the warning in the interface.
- Any other Day Plan limit.
