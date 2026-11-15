"""Fill the Markdown templates in ``docs/templates``: the template is the structure.

A template holds the front matter, headings, tables and fixed sentences of a generated file.
The code supplies only values: ``{{name}}`` is replaced by the value of that name, and
``{# ... #}`` is a comment for whoever edits the template and is dropped. Values are inserted as
given and never scanned again, so ticket text that looks like a placeholder stays as typed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "docs" / "templates"

_COMMENT = re.compile(r"\{#.*?#\}[ \t]*\n?", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_ONLY_PLACEHOLDER = re.compile(r"^\s*\{\{\s*(\w+)\s*\}\}\s*$")


class TemplateError(ValueError):
    """A template names a placeholder the code does not supply."""


def render(name: str, values: Mapping[str, str], templates_dir: Path | None = None) -> str:
    """Render ``<templates_dir>/<name>`` with the given values.

    Raises ``TemplateError`` for a placeholder with no value. A line that holds only a placeholder
    whose value is empty is dropped, and trailing spaces are trimmed from each filled line.
    """
    text = (templates_dir or TEMPLATES_DIR).joinpath(name).read_text(encoding="utf-8")
    template = _COMMENT.sub("", text)
    missing = sorted(set(_PLACEHOLDER.findall(template)) - set(values))
    if missing:
        raise TemplateError(f"{name} uses {', '.join(missing)}, which the code does not supply")
    lines = []
    for line in template.splitlines():
        alone = _ONLY_PLACEHOLDER.match(line)
        if alone and values[alone.group(1)] == "":
            continue
        lines.append(_PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), line).rstrip(" \t"))
    return "\n".join(lines).rstrip("\n") + "\n"
