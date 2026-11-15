---
ticket: 1
stage: spec
requirements: [REQ-8]
---
# Export a Trip as a plain-text Itinerary

## Summary
A Trip can produce its Itinerary as plain text. The first line is the Trip name. Each Day Plan then starts a section headed `Day N`, counting from 1 in the order the Day Plans were added, followed by one `- <Place name> (<Duration> min)` line per Stop. A Trip with no Day Plans produces just its name. The text contains nothing else: no export time and no coordinates.

## Acceptance criteria
1. Given a Trip named "Lisbon" with no Day Plans, When the Itinerary is exported, Then the text is exactly "Lisbon".
2. Given a Trip with two Day Plans, When the Itinerary is exported, Then the sections are headed "Day 1" and "Day 2".
3. Given a Day Plan with a Stop at "Alfama" for 90 minutes, When the Itinerary is exported, Then it contains the line "- Alfama (90 min)".
4. Given the same Trip, When the Itinerary is exported twice, Then both texts are identical.

## Out of scope
- Dates in the text, file saving and sharing.
