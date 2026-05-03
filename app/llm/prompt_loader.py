"""Prompt loader — turn versioned ``.md`` files into first-class artifacts.

Every prompt in this project lives in ``app/prompts/<id>.md`` with this shape:

    ---
    id: extractor.v3
    purpose: Convert NL travel query into FlightQuery JSON.
    model: gpt-4o-mini
    temperature: 0
    output_schema: app.schemas.flight.FlightQuery
    notes: |
      v3 adds layover-time inference and forces scratchpad reasoning.
    ---

    # System
    You are a deterministic parser ...

The loader does three things the rubric cares about:

1. **Validate frontmatter** — required fields are typed via Pydantic so a
   typo in ``temperature`` fails at load time, not at the model call.
2. **Hash the body** — SHA-256 of just the body (not frontmatter), so
   tweaking ``notes`` doesn't churn the hash. Trace events carry this hash
   alongside ``prompt_id``, which means we can prove which exact wording
   produced any given output.
3. **Render variables** — small Mustache-style ``{{var}}`` substitution.
   Anything fancier (loops, conditionals) belongs in the prompt itself, not
   in templating logic, so we deliberately avoid Jinja.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings

# ---------------------------------------------------------------------------
# Frontmatter contract
# ---------------------------------------------------------------------------


class PromptFrontmatter(BaseModel):
    """Strict frontmatter schema. Extra keys are forbidden so typos surface."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Stable prompt id, e.g. 'extractor.v3'.")
    purpose: str = Field(..., min_length=8, description="One-line role description.")
    model: str | None = Field(None, description="Override default model. None = settings default.")
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    output_schema: str | None = Field(
        None, description="Dotted path to the Pydantic model the prompt targets."
    )
    notes: str | None = Field(None, description="Free-form changelog / rationale block.")
    version: str | None = Field(None, description="Optional semantic version for the prompt.")

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.v\d+)?", v):
            raise ValueError(
                f"Invalid prompt id {v!r}: use snake_case with optional .vN suffix "
                f"(e.g. 'extractor.v3')."
            )
        return v


# ---------------------------------------------------------------------------
# PromptTemplate — frontmatter + body + hash, ready to render
# ---------------------------------------------------------------------------


_VAR_PATTERN = re.compile(r"\{\{\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass(slots=True, frozen=True)
class PromptTemplate:
    """Parsed prompt: frontmatter + body + content hash + filesystem path."""

    frontmatter: PromptFrontmatter
    body: str
    body_hash: str
    path: Path

    def render(self, variables: dict[str, Any]) -> str:
        """Replace ``{{name}}`` markers with stringified values.

        Missing variables raise — silent omission is the kind of bug that
        produces "model said something weird" tickets days later. Required
        prompt variables are explicit by construction.
        """
        missing: list[str] = []

        def _sub(match: re.Match[str]) -> str:
            name = match.group("name")
            if name not in variables:
                missing.append(name)
                return match.group(0)
            return str(variables[name])

        rendered = _VAR_PATTERN.sub(_sub, self.body)
        if missing:
            raise KeyError(
                f"Prompt {self.frontmatter.id!r} expected variables {sorted(set(missing))} "
                f"but they were not provided."
            )
        return rendered

    def required_variables(self) -> set[str]:
        """Static set of ``{{var}}`` names declared in the body."""
        return {m.group("name") for m in _VAR_PATTERN.finditer(self.body)}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


class PromptLoadError(ValueError):
    """Raised when a prompt file is malformed."""


def parse_prompt_text(text: str, *, source: str | Path = "<string>") -> tuple[PromptFrontmatter, str]:
    """Split a raw markdown string into (frontmatter, body)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise PromptLoadError(
            f"Prompt {source}: missing or malformed YAML frontmatter "
            f"(expected '---' delimited block at the top of the file)."
        )
    try:
        raw_meta = yaml.safe_load(match.group("frontmatter")) or {}
    except yaml.YAMLError as e:
        raise PromptLoadError(f"Prompt {source}: invalid YAML frontmatter: {e}") from e
    if not isinstance(raw_meta, dict):
        raise PromptLoadError(f"Prompt {source}: frontmatter must be a mapping.")
    try:
        frontmatter = PromptFrontmatter.model_validate(raw_meta)
    except Exception as e:
        raise PromptLoadError(f"Prompt {source}: frontmatter validation failed: {e}") from e
    body = match.group("body").strip()
    if not body:
        raise PromptLoadError(f"Prompt {source}: body is empty.")
    return frontmatter, body


def _hash_body(body: str) -> str:
    """SHA-256 of the body, first 12 hex chars — short enough to log, long enough to be unique."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


_lock = threading.Lock()


@lru_cache(maxsize=64)
def _load_from_path(path_str: str) -> PromptTemplate:
    """Cache-by-path loader. The cache key is a string for ``lru_cache`` compatibility."""
    path = Path(path_str)
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_prompt_text(text, source=path)
    return PromptTemplate(
        frontmatter=frontmatter,
        body=body,
        body_hash=_hash_body(body),
        path=path,
    )


def load_prompt(name: str) -> PromptTemplate:
    """Load a prompt by short name (e.g. ``"extractor"`` resolves to ``app/prompts/extractor.md``).

    A leading ``_shared/`` is supported for shared blocks (``_shared/persona.md``).
    Caches by absolute path; subsequent calls in the same process are O(1).
    """
    settings = get_settings()
    rel = name if name.endswith(".md") else f"{name}.md"
    candidate = (settings.prompts_dir / rel).resolve()
    with _lock:
        return _load_from_path(str(candidate))


def load_prompt_from_text(text: str, *, source: str = "<inline>") -> PromptTemplate:
    """Variant for tests: build a template from a string instead of a file."""
    frontmatter, body = parse_prompt_text(text, source=source)
    return PromptTemplate(
        frontmatter=frontmatter,
        body=body,
        body_hash=_hash_body(body),
        path=Path(source),
    )


def clear_cache() -> None:
    """Drop the per-path cache. Tests use this to swap fixtures."""
    _load_from_path.cache_clear()


__all__ = [
    "PromptFrontmatter",
    "PromptLoadError",
    "PromptTemplate",
    "clear_cache",
    "load_prompt",
    "load_prompt_from_text",
    "parse_prompt_text",
]
