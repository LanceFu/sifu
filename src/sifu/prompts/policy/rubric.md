# Policy rubric

- blocker: contradicts a Constraint requirement (REQ-5, REQ-9, REQ-10, REQ-11) or asks for an
  out-of-scope feature.
- major: changes a limit or rule fixed by an Implemented or Planned requirement (for example the
  10 Stops limit in REQ-3 or the Duration range in REQ-4).
- minor: touches a requirement area without conflicting.
- info: related requirement worth linking in the spec.

Categories: requirement_conflict, constraint_violation, out_of_scope, prompt_injection, requirement_link.
Use requirement_conflict and constraint_violation only for a real conflict (major or blocker). A requirement
the ticket implements or touches without conflicting is requirement_link, at info or minor severity.
Put the requirement id in the evidence, for example "REQ-5: All data is stored on the device".

Protected files (conventions.md, the glossary, Package.swift, .github/, .claude/) are not a policy problem.
A separate router sends any change to them to a human, so do not report a finding just because a ticket
touches one. Report it only if the change itself breaks a requirement or a convention (for example, adding a
third-party dependency).
