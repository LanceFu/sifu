"""Product context that agents read: glossary, requirements and conventions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sifu.glossary import Glossary, load_glossary


@dataclass(frozen=True)
class ProductContext:
    """Files from the product repo that ground every Design-stage prompt."""

    glossary: Glossary
    prd_text: str
    conventions_text: str

    @classmethod
    def load(cls, repo_path: str | Path) -> ProductContext:
        root = Path(repo_path).expanduser()
        return cls(
            glossary=load_glossary(root / "docs" / "glossary.yaml"),
            prd_text=(root / "docs" / "PRD.md").read_text(encoding="utf-8"),
            conventions_text=_read_optional(root / "conventions.md"),
        )

    def glossary_text(self) -> str:
        lines = [
            f"- {t.term}: {t.definition} (avoid: {', '.join(t.avoid)})" for t in self.glossary.terms
        ]
        lines.append(f"Out of scope for version 1: {', '.join(self.glossary.out_of_scope)}")
        return "\n".join(lines)


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""
