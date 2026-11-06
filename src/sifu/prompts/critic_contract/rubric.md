## Rubric
- blocker: an acceptance criterion is not met, or the code does the opposite.
- major: the code adds behaviour or a public API the spec does not ask for, or reports the wrong error.
- minor: the behaviour matches but a spec term is used loosely in a public name.
For every finding name the criterion in the message.
Reply with one JSON object only: {"findings": [...]}. Each finding has severity (blocker, major or minor), category, file, line, message and scenario. "file" and "line" must point at a line listed in the diff, which shows only the lines the change added. "scenario" is a concrete input or sequence of calls that makes the code fail, in at least a full sentence. If you cannot name a scenario, do not report the finding. Return {"findings": []} when you see nothing.
