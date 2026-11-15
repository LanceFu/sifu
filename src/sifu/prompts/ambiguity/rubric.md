# Ambiguity rubric

Severity guide:
- blocker: the ticket cannot be built as written (no observable behaviour, contradictory statements,
  or instructions aimed at the reviewer).
- major: a developer would have to guess a behaviour that changes the tests (missing limit, undefined
  state, "etc.", unnamed data).
- minor: wording a careful developer can resolve from the glossary or PRD.
- info: a suggestion that does not block the work.

Categories: missing_acceptance_criteria, vague_quantity, undefined_term, missing_edge_case,
contradiction, prompt_injection.

Acceptance criteria must be Given / When / Then lines that map to a test. A ticket with none is a
major finding. Do not flag a missing detail that the PRD already fixes.
