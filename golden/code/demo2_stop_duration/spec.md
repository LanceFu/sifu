---
ticket: 1
stage: spec
requirements: [REQ-4]
---
# Validate a Stop's Duration

## Summary
Each Stop has a Duration between 5 and 720 minutes, inclusive. A Stop can be created from a number of minutes or from the text a Traveller typed. An invalid Duration fails with a `durationOutOfRange` error (a case of a typed Error enum).

## Acceptance criteria
1. Given a Duration below 5 or above 720 minutes, When a Stop is created, Then creation fails with a `durationOutOfRange` error.
2. Given a Duration from 5 to 720 minutes inclusive, When a Stop is created, Then creation succeeds with that Duration.
3. Given a Traveller types a Duration as text, When the text is not a whole number, Then creation fails with a `durationOutOfRange` error and nothing crashes.

## Out of scope
- Changing the Duration of an existing Stop.
- Any change to how Durations are displayed.
