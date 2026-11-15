"""Lessons: short rules learned when a human overrides a decision, kept in conventions.md.

``conventions.md`` is a protected file in the product repo, so Sifu never edits the approved copy.
Lessons are appended to a proposed copy that a human-reviewed pull request will later merge.
"""

from __future__ import annotations

from pathlib import Path

HEADING = "## Lessons"


def append_lesson(path: str | Path, lesson: str, ticket_id: int | None = None) -> bool:
    """Append one bullet under the Lessons heading. Returns False if the bullet already exists."""
    target = Path(path)
    text = target.read_text(encoding="utf-8") if target.exists() else "# Conventions\n"
    if HEADING not in text:
        text = text.rstrip("\n") + f"\n\n{HEADING}\n"
    prefix = f"(ticket {ticket_id}) " if ticket_id is not None else ""
    bullet = f"- {prefix}{' '.join(lesson.split())}"
    if bullet in text.splitlines():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip("\n") + "\n" + bullet + "\n", encoding="utf-8")
    return True


def propose_lesson(
    approved: str | Path, proposed: str | Path, lesson: str, ticket_id: int | None = None
) -> bool:
    """Seed the proposed copy from the approved conventions the first time, then append."""
    approved_path, proposed_path = Path(approved), Path(proposed)
    if not proposed_path.exists() and approved_path.exists():
        proposed_path.parent.mkdir(parents=True, exist_ok=True)
        proposed_path.write_text(approved_path.read_text(encoding="utf-8"), encoding="utf-8")
    return append_lesson(proposed_path, lesson, ticket_id)
