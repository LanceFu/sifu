---
ticket: 1
stage: spec
requirements: [REQ-6]
---
# Duplicate a Trip

## Summary
A Trip can be duplicated under a new name. The copy has the same dates and a copy of every Day Plan and Stop. The copy and everything in it get new ids, so changing the copy never touches the original. A blank new name fails with the same `nameBlank` error as creating a Trip.

## Acceptance criteria
1. Given a Trip with three Day Plans, When it is duplicated, Then the copy has three Day Plans with the same dates and Stops.
2. Given a duplicated Trip, When ids are compared, Then the Trip, each Day Plan and each Stop have a different id from the original's.
3. Given a blank new name, When a Trip is duplicated, Then it fails with `nameBlank`.

## Out of scope
- Copying anything else, and merging Trips.
