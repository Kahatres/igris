from pathlib import Path

import pytest

import igris.personas as personas_module
from igris.personas import (
    BUILTIN,
    Persona,
    PersonaError,
    delete_persona,
    load_personas,
    parse_persona,
    persona_prompt,
    render_persona,
    save_persona,
)


def test_builtins_exist():
    assert BUILTIN["default"].prompt == ""
    assert "coder" in BUILTIN
    turkish = BUILTIN["turkish"]
    assert turkish.language == "tr"
    assert "Turkish" in turkish.prompt


def test_parse_front_matter():
    text = '---\ndescription: "A pirate"\nlanguage: \'en\'\n---\nTalk like a pirate.\n'
    p = parse_persona(text, "pirate")
    assert p == Persona("pirate", "A pirate", "Talk like a pirate.", "en")


def test_parse_without_front_matter():
    p = parse_persona("Just be nice.\n", "nice")
    assert p.prompt == "Just be nice."
    assert p.description == ""
    assert p.language == ""


def test_parse_unclosed_front_matter_is_body():
    p = parse_persona("---\nhello", "x")
    assert "hello" in p.prompt


@pytest.mark.parametrize("name", ["", "a b", "a/b", "x" * 41, "bad.name", "a\n"])
def test_invalid_names(name):
    with pytest.raises(PersonaError):
        parse_persona("body", name)


def test_empty_body_raises():
    with pytest.raises(PersonaError):
        parse_persona("---\ndescription: d\n---\n   \n", "empty")


def test_load_none_and_missing(tmp_path):
    assert load_personas(None) == BUILTIN
    assert load_personas(tmp_path / "nope") == BUILTIN


def test_load_returns_new_dict(tmp_path):
    a = load_personas(None)
    a["zzz"] = Persona("zzz", "", "p")
    assert "zzz" not in load_personas(None)
    assert "zzz" not in BUILTIN


def test_files_override_builtins_and_add(tmp_path):
    (tmp_path / "coder.md").write_text(
        "---\ndescription: custom\n---\nCustom coder.", encoding="utf-8"
    )
    (tmp_path / "poet.md").write_text("Write poems.", encoding="utf-8")
    result = load_personas(tmp_path)
    assert result["coder"].prompt == "Custom coder."
    assert result["coder"].description == "custom"
    assert result["poet"].prompt == "Write poems."
    assert "turkish" in result


def test_invalid_files_skipped(tmp_path):
    (tmp_path / "empty.md").write_text("---\nlanguage: tr\n---\n", encoding="utf-8")
    (tmp_path / "bad name.md").write_text("body", encoding="utf-8")
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00\x80\x81")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "dir.md").mkdir()
    (tmp_path / "good.md").write_text("Good one.", encoding="utf-8")
    result = load_personas(tmp_path)
    assert set(result) == set(BUILTIN) | {"good"}


def test_persona_prompt():
    assert persona_prompt(BUILTIN["default"]) == ""
    assert persona_prompt(Persona("a", "", "Be brief.")) == "Persona: a\nBe brief."
    assert (
        persona_prompt(Persona("a", "", "Be brief.", "tr"))
        == "Persona: a\nBe brief.\nAlways answer in tr."
    )


@pytest.mark.parametrize(
    "persona",
    [
        Persona("turkce", "Türkçe yardımcı", "İyi düşün.\nKısa ve açık yanıt ver.", "tr"),
        Persona("plain", "", "Be helpful."),
        Persona("quotes", '  "Quoted": \'value\' # text  ', "---\nA prompt.", " en "),
        Persona("escapes", r"C:\new\test", r"Keep \n literal.", r"\tr"),
        Persona("multiline", "First\nSecond\r\nThird", "Answer.", "en\ntr"),
        Persona("whitespace", "Description", " \n\tKeep whitespace.\r\n "),
        Persona("bom", "Description", "\ufeffKeep the body BOM."),
        Persona("unicode", "A\u2028B\u0085C", "A\u2029B", "tr"),
    ],
)
def test_render_round_trip(persona):
    rendered = render_persona(persona)
    assert rendered.startswith("---\ndescription:")
    assert "\n---\n\n" in rendered
    assert rendered.endswith(persona.prompt)
    assert parse_persona(rendered, persona.name) == persona
    if persona.language:
        assert "\nlanguage:" in rendered
    else:
        assert "\nlanguage:" not in rendered


def test_legacy_metadata_escapes_and_body_whitespace_are_unchanged():
    persona = parse_persona(
        '---\ndescription: "Literal\\ntext"\n---\n\n  Answer.  \n',
        "legacy",
    )
    assert persona.description == r"Literal\ntext"
    assert persona.prompt == "Answer."


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../outside", "a/b", r"a\b", "/absolute", "C:drive", "a\n", "x" * 41, None],
)
def test_file_operations_reject_invalid_names(tmp_path, name):
    directory = tmp_path / "personas"
    persona = Persona(name, "", "Answer.")
    with pytest.raises(PersonaError):
        render_persona(persona)
    with pytest.raises(PersonaError):
        save_persona(directory, persona, overwrite=True)
    with pytest.raises(PersonaError):
        delete_persona(directory, name)
    assert not directory.exists()


@pytest.mark.parametrize("prompt", ["", " ", "\t\r\n", None])
def test_save_rejects_empty_prompt_before_creating_directory(tmp_path, prompt):
    directory = tmp_path / "personas"
    persona = Persona("empty", "", prompt)
    with pytest.raises(PersonaError):
        render_persona(persona)
    with pytest.raises(PersonaError):
        save_persona(directory, persona)
    assert not directory.exists()


@pytest.mark.parametrize(
    "persona",
    [
        Persona("turkce", "Türkçe yardımcı", "Türkçe yanıt ver.", "tr"),
        Persona("exact", "First\nSecond", " \r\nKeep exact whitespace.\r\n ", "tr"),
    ],
)
def test_save_creates_directory_and_loads_persona(tmp_path, persona):
    directory = tmp_path / "nested" / "personas"
    path = save_persona(directory, persona)
    assert path == directory / f"{persona.name}.md"
    assert path.read_bytes() == render_persona(persona).encode("utf-8")
    assert load_personas(directory)[persona.name] == persona
    assert set(directory.iterdir()) == {path}


def test_save_existing_file_requires_overwrite(tmp_path):
    original = Persona("helper", "Original", "Original prompt.")
    replacement = Persona("helper", "Replacement", "Replacement prompt.")
    path = save_persona(tmp_path, original)
    original_bytes = path.read_bytes()

    with pytest.raises(PersonaError):
        save_persona(tmp_path, replacement)
    assert path.read_bytes() == original_bytes

    assert save_persona(tmp_path, replacement, overwrite=True) == path
    assert load_personas(tmp_path)["helper"] == replacement
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("name", sorted(BUILTIN))
def test_builtin_save_protection_and_local_override_deletion(tmp_path, name):
    original = BUILTIN[name]
    persona = Persona(name, "Local override", "Custom prompt.", "tr")
    directory = tmp_path / "personas"

    with pytest.raises(PersonaError):
        save_persona(directory, persona)
    assert not directory.exists()
    with pytest.raises(PersonaError):
        delete_persona(directory, name)

    path = save_persona(directory, persona, overwrite=True)
    assert load_personas(directory)[name] == persona
    with pytest.raises(PersonaError):
        save_persona(directory, persona)

    delete_persona(directory, name)
    assert not path.exists()
    assert load_personas(directory)[name] == original
    assert BUILTIN[name] == original
    with pytest.raises(PersonaError):
        delete_persona(directory, name)


def test_delete_custom_persona_and_missing_file(tmp_path):
    path = save_persona(tmp_path, Persona("custom", "", "Custom prompt."))
    unrelated = tmp_path / "keep.md"
    unrelated.write_text("Keep this.", encoding="utf-8")

    delete_persona(tmp_path, "custom")
    assert not path.exists()
    assert "custom" not in load_personas(tmp_path)
    assert unrelated.read_text(encoding="utf-8") == "Keep this."
    with pytest.raises(PersonaError):
        delete_persona(tmp_path, "custom")


def test_delete_traversal_does_not_touch_outside_file(tmp_path):
    directory = tmp_path / "personas"
    directory.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Keep this.", encoding="utf-8")

    with pytest.raises(PersonaError):
        delete_persona(directory, "../outside")
    assert outside.read_text(encoding="utf-8") == "Keep this."


def test_save_uses_replace_with_complete_utf8_temporary_file(tmp_path, monkeypatch):
    persona = Persona("turkce", "Açıklama", "Türkçe konuş.", "tr")
    destination = tmp_path / "turkce.md"
    destination.write_text("Old prompt.", encoding="utf-8")
    real_replace = personas_module.os.replace
    calls = []

    def checked_replace(source, target):
        source = Path(source)
        target = Path(target)
        assert source.parent == tmp_path
        assert source.suffix == ".tmp"
        assert source.read_bytes() == render_persona(persona).encode("utf-8")
        assert target == destination
        assert destination.read_text(encoding="utf-8") == "Old prompt."
        calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(personas_module.os, "replace", checked_replace)
    save_persona(tmp_path, persona, overwrite=True)
    assert len(calls) == 1
    assert load_personas(tmp_path)["turkce"] == persona
    assert set(tmp_path.iterdir()) == {destination}


def test_failed_replace_preserves_original_and_removes_temporary_file(tmp_path, monkeypatch):
    original = Persona("helper", "", "Original prompt.")
    destination = save_persona(tmp_path, original)
    original_bytes = destination.read_bytes()

    def fail_replace(source, target):
        assert Path(source).is_file()
        raise OSError("Simulated replacement failure")

    monkeypatch.setattr(personas_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="Simulated replacement failure"):
        save_persona(
            tmp_path, Persona("helper", "", "Replacement prompt."), overwrite=True
        )
    assert destination.read_bytes() == original_bytes
    assert set(tmp_path.iterdir()) == {destination}


def test_symlinks_never_modify_outside_targets(tmp_path):
    directory = tmp_path / "personas"
    directory.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Outside prompt.", encoding="utf-8")
    link = directory / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable")

    persona = Persona("linked", "", "Local replacement.")
    with pytest.raises(PersonaError):
        save_persona(directory, persona)
    save_persona(directory, persona, overwrite=True)
    assert not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "Outside prompt."

    link.unlink()
    link.symlink_to(outside)
    delete_persona(directory, "linked")
    assert not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "Outside prompt."

    link.symlink_to(tmp_path / "missing.md")
    with pytest.raises(PersonaError):
        save_persona(directory, persona)
    delete_persona(directory, "linked")
    assert not link.is_symlink()
