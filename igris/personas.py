"""System prompt personas: built-in and file-based."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


@dataclass(frozen=True)
class Persona:
    name: str
    description: str
    prompt: str  # text appended to the assistant's system prompt
    language: str = ""  # optional answer language, e.g. "tr"


class PersonaError(ValueError):
    """Raised when a persona definition is invalid."""


BUILTIN: dict[str, Persona] = {
    "default": Persona("default", "Default assistant behaviour", ""),
    "coder": Persona(
        "coder",
        "Concise, practical programming helper",
        "You are an expert software engineer. Give correct, minimal, "
        "well-explained code and point out edge cases.",
    ),
    "turkish": Persona(
        "turkish",
        "Answers in Turkish",
        "Respond to the user in Turkish.",
        "tr",
    ),
}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            meta: dict[str, str] = {}
            for line in lines[1:idx]:
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = _unquote(value)
            if meta.get("igris-format") == "exact-v1":
                # Rendered noncanonical values use explicit JSON metadata.
                for line in lines[1:idx]:
                    key, separator, value = line.partition(":")
                    key = key.strip().lower()
                    if separator and key in {"description", "language"}:
                        try:
                            decoded = json.loads(value.strip())
                        except ValueError as exc:
                            raise PersonaError("invalid encoded persona metadata") from exc
                        if not isinstance(decoded, str):
                            raise PersonaError("persona metadata must contain strings")
                        meta[key] = decoded
                body = "".join(text.splitlines(keepends=True)[idx + 1:])
                if not body.startswith("\n"):
                    raise PersonaError("encoded persona requires a blank separator line")
                return meta, body[1:]
            return meta, "\n".join(lines[idx + 1:])
    # No closing marker: treat everything as the body.
    return {}, text


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise PersonaError(f"invalid persona name: {name!r}")


def parse_persona(text: str, name: str) -> Persona:
    """Parse persona file text; the name comes from the file stem."""
    _validate_name(name)
    meta, body = _split_front_matter(text)
    if meta.get("igris-format") != "exact-v1":
        body = body.strip()
    if not body.strip():
        raise PersonaError(f"persona {name!r} has an empty prompt")
    return Persona(
        name=name,
        description=meta.get("description", ""),
        prompt=body,
        language=meta.get("language", ""),
    )


def render_persona(persona: Persona) -> str:
    """Render front matter and a prompt without losing string contents.

    Ordinary definitions retain the existing human-readable format. An explicit
    format marker preserves multiline metadata and noncanonical whitespace,
    without changing whitespace handling for existing persona files.
    """
    _validate_name(persona.name)
    if not isinstance(persona.prompt, str) or not persona.prompt.strip():
        raise PersonaError(f"persona {persona.name!r} has an empty prompt")
    if not isinstance(persona.description, str) or not isinstance(persona.language, str):
        raise PersonaError("persona metadata must contain strings")

    line_breaks = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    exact = (
        persona.prompt != persona.prompt.strip()
        or persona.prompt != "\n".join(persona.prompt.splitlines())
        or any(char in persona.description + persona.language for char in line_breaks)
    )

    def quote(value: str) -> str:
        # ASCII JSON also escapes Unicode characters recognized by splitlines.
        return json.dumps(value, ensure_ascii=True) if exact else f'"{value}"'

    lines = ["---", f"description: {quote(persona.description)}"]
    if persona.language:
        lines.append(f"language: {quote(persona.language)}")
    if exact:
        lines.append("igris-format: exact-v1")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + persona.prompt


def save_persona(
    directory: Path, persona: Persona, overwrite: bool = False
) -> Path:
    """Atomically save a validated persona using a private temporary file."""
    text = render_persona(persona)
    directory = Path(directory)
    path = directory / f"{persona.name}.md"

    def check_conflict() -> None:
        if not overwrite and (
            persona.name in BUILTIN or path.exists() or path.is_symlink()
        ):
            raise PersonaError(f"persona {persona.name!r} already exists")

    check_conflict()
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=directory,
            prefix=f".{persona.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        check_conflict()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def delete_persona(directory: Path, name: str) -> None:
    """Delete only the named local file, leaving built-in definitions intact."""
    _validate_name(name)
    path = Path(directory) / f"{name}.md"
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise PersonaError(f"persona {name!r} has no local file") from exc


def load_personas(directory: Path | None) -> dict[str, Persona]:
    """Return BUILTIN merged with <directory>/*.md (files win)."""
    result = dict(BUILTIN)
    if directory is None:
        return result
    try:
        directory = Path(directory)
        if not directory.is_dir():
            return result
        files = sorted(directory.glob("*.md"))
    except OSError:
        return result
    for path in files:
        try:
            if not path.is_file():
                continue
            with path.open(encoding="utf-8", newline="") as stream:
                persona = parse_persona(stream.read(), path.stem)
        except (OSError, UnicodeError, PersonaError):
            continue
        result[persona.name] = persona
    return result


def persona_prompt(persona: Persona) -> str:
    """Return the block appended to the system prompt ("" if no prompt)."""
    if not persona.prompt:
        return ""
    block = f"Persona: {persona.name}\n{persona.prompt}"
    if persona.language:
        block += f"\nAlways answer in {persona.language}."
    return block
