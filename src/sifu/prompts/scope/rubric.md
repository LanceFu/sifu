# Scope and sizing rubric

- blocker: depends on a feature that does not exist and is not part of the ticket.
- major: two or more independent behaviours in one ticket, or an estimated change over about
  400 changed lines or more than 8 files.
- minor: one behaviour plus a small unrelated cleanup.
- info: could be split but does not need to be.

Categories: oversized, bundled_behaviours, missing_dependency, prompt_injection.
Count each "and also" in the ticket as a possible second behaviour. Check the PRD status column:
a requirement marked Planned may depend on another Planned requirement.
