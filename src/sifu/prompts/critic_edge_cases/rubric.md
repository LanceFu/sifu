## Rubric
- blocker: a plausible input produces a wrong result, a crash or silent data loss, and no test covers it.
- major: a boundary behaves differently from what the acceptance criteria imply.
- minor: a rare input is handled in a surprising but harmless way.
Never report an issue the tests already cover or one that needs an input the types make impossible.
Reply with one JSON object only: {"findings": [...]}. Each finding has severity (blocker, major or minor), category, file, line, message and scenario. "file" and "line" must point at a line listed in the diff, which shows only the lines the change added. "scenario" is a concrete input or sequence of calls that makes the code fail, in at least a full sentence. If you cannot name a scenario, do not report the finding. Return {"findings": []} when you see nothing.
