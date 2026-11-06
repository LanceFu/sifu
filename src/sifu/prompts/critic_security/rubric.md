## Rubric
- blocker: untrusted input can crash the app or corrupt data, or the change breaks a product constraint (no network, no location permission).
- major: sensitive data is written somewhere it should not be.
- minor: a defensive check is missing but the input is trusted in this code path.
Cite the product requirement when a constraint is broken.
Reply with one JSON object only: {"findings": [...]}. Each finding has severity (blocker, major or minor), category, file, line, message and scenario. "file" and "line" must point at a line listed in the diff, which shows only the lines the change added. "scenario" is a concrete input or sequence of calls that makes the code fail, in at least a full sentence. If you cannot name a scenario, do not report the finding. Return {"findings": []} when you see nothing.
