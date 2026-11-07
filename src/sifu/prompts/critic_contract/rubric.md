## Rubric
- blocker: an acceptance criterion is not met, or the code does the opposite.
- major: the code adds behaviour or a public API the spec does not ask for, or reports the wrong error.
- minor: the behaviour matches but a spec term is used loosely in a public name.
For every finding name the criterion in the message.
Before you say the code omits or skips something, read every row shown for that file, context rows included: the behaviour may be on the next, unchanged line. Report a missing behaviour only if no shown row does it.
If the code breaks a criterion because of a boundary mistake (an off-by-one), report it once against the line with the mistake.
Reply with one JSON object only: {"findings": [...]}. Each finding has severity (blocker, major or minor), category, file, line, quote, message and scenario. Rows marked `+` were added by the change; unchanged lines around them are shown as context with a blank mark. "file" and "line" must point at a `+` row. "quote" is a short piece of text copied exactly from that line; code rejects the finding if the quote is not on the cited line, so cite the `+` line that shows the problem, not a neighbour. "scenario" is a concrete input or sequence of calls that makes the code fail, in at least a full sentence. If you cannot name a scenario, do not report the finding. Return {"findings": []} when you see nothing.
