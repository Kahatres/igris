from pathlib import Path

import pytest

from igris.config import Settings, load_settings, parse_dotenv


def test_account_settings(tmp_path):
    settings = load_settings({"IGRIS_DATA_DIR": str(tmp_path), "IGRIS_USER_BACKENDS": "echo",
                              "IGRIS_PUBLIC_ORIGIN": "https://igris.example.com"})
    assert settings.data_dir == tmp_path and settings.user_backends == ("echo",)
    assert settings.public_origin == "https://igris.example.com"


@pytest.mark.parametrize("origin", ["http://example.com", "https://example.com/path",
                                   "https://user:password@example.com", "https://"])
def test_public_origin_validation(origin):
    with pytest.raises(ValueError):
        load_settings({"IGRIS_PUBLIC_ORIGIN": origin})


def test_regular_user_cli_backends_rejected():
    with pytest.raises(ValueError):
        load_settings({"IGRIS_USER_BACKENDS": "codex"})


def test_opencode_settings_and_owner_only_rule():
    from igris.config import ALLOWED_BACKENDS, USER_SAFE_BACKENDS

    assert "opencode" in ALLOWED_BACKENDS
    assert "opencode" not in USER_SAFE_BACKENDS
    assert load_settings({"IGRIS_BACKENDS": "opencode"}).backends == ("opencode",)
    assert load_settings({}).opencode_model == "deepseek-v4.1-flash-free"
    assert load_settings({"IGRIS_OPENCODE_MODEL": "glm-5.3-flash-free"}).opencode_model == "glm-5.3-flash-free"
    with pytest.raises(ValueError) as error:
        load_settings({"IGRIS_USER_BACKENDS": "opencode"})
    with pytest.raises(ValueError) as existing:
        load_settings({"IGRIS_USER_BACKENDS": "codex"})
    assert str(error.value) == str(existing.value)


def test_unknown_opencode_model_names_allowed_models():
    from igris.llm_opencode import FREE_MODELS

    with pytest.raises(ValueError) as error:
        load_settings({"IGRIS_OPENCODE_MODEL": "paid-model"})
    assert "IGRIS_OPENCODE_MODEL" in str(error.value)
    assert all(model in str(error.value) for model in FREE_MODELS)


def test_task_worker_limit():
    assert load_settings({"IGRIS_TASK_WORKERS": "3"}).task_workers == 3
    assert load_settings({}).allow_training_providers is False
    assert load_settings({"IGRIS_ALLOW_TRAINING_PROVIDERS": "1"}).allow_training_providers is True
    with pytest.raises(ValueError):
        load_settings({"IGRIS_ALLOW_TRAINING_PROVIDERS": "maybe"})
    for value in ("0", "5", "bad"):
        with pytest.raises(ValueError):
            load_settings({"IGRIS_TASK_WORKERS": value})


def test_defaults():
    s = load_settings({})
    assert s == Settings()
    assert s.backends == ("claude", "codex")
    assert s.max_history == 20
    assert s.timeout == 300


def test_env_none_uses_os_environ(monkeypatch):
    monkeypatch.setenv("IGRIS_NAME", "Zed")
    assert load_settings().name == "Zed"


def test_env_overrides():
    s = load_settings(
        {
            "IGRIS_NAME": "X",
            "IGRIS_BACKENDS": " Echo, ,CLAUDE ",
            "IGRIS_CLAUDE_MODEL": "opus",
            "IGRIS_CODEX_MODEL": "m",
            "IGRIS_MEMORY_PATH": "/tmp/m.jsonl",
            "IGRIS_MAX_HISTORY": "5",
            "IGRIS_TIMEOUT": "10",
        }
    )
    assert s.name == "X"
    assert s.backends == ("echo", "claude")
    assert s.claude_model == "opus"
    assert s.codex_model == "m"
    assert s.memory_path == Path("/tmp/m.jsonl")
    assert s.max_history == 5
    assert s.timeout == 10


def test_dotenv_and_env_precedence(tmp_path):
    f = tmp_path / ".env"
    f.write_text("IGRIS_NAME=FromFile\nIGRIS_TIMEOUT=50\n")
    s = load_settings({"IGRIS_NAME": "FromEnv"}, f)
    assert s.name == "FromEnv"
    assert s.timeout == 50


def test_missing_dotenv_ok(tmp_path):
    assert load_settings({}, tmp_path / "nope.env") == Settings()


def test_parse_dotenv():
    text = (
        "# comment\n\n"
        "A=1\n"
        'B="hello # not comment"\n'
        "C='single'\n"
        "D=value # trailing\n"
        "novalue\n"
        "E=\n"
        "F=a#b\n"
    )
    assert parse_dotenv(text) == {
        "A": "1",
        "B": "hello # not comment",
        "C": "single",
        "D": "value",
        "E": "",
        "F": "a#b",
    }


@pytest.mark.parametrize("var", ["IGRIS_MAX_HISTORY", "IGRIS_TIMEOUT"])
@pytest.mark.parametrize("bad", ["0", "-3", "abc", "", "1.5"])
def test_int_validation(var, bad):
    with pytest.raises(ValueError, match=var):
        load_settings({var: bad})


@pytest.mark.parametrize("bad", ["foo", "claude,bogus", "", " , "])
def test_backend_validation(bad):
    with pytest.raises(ValueError, match="IGRIS_BACKENDS"):
        load_settings({"IGRIS_BACKENDS": bad})


def test_new_defaults():
    s = load_settings({})
    assert s.persona == "default"
    assert s.sessions_dir == s.data_dir / "sessions"
    assert s.personas_dir == s.data_dir / "personas"
    assert s.workspace is None


def test_new_overrides():
    s = load_settings(
        {
            "IGRIS_PERSONA": "coder",
            "IGRIS_SESSIONS_DIR": "/tmp/s",
            "IGRIS_PERSONAS_DIR": "/tmp/p",
            "IGRIS_WORKSPACE": "/tmp/w",
        }
    )
    assert s.persona == "coder"
    assert s.sessions_dir == Path("/tmp/s")
    assert s.personas_dir == Path("/tmp/p")
    assert s.workspace == Path("/tmp/w")


def test_multiagent_dir_default_and_override():
    assert load_settings({}).multiagent_dir is None
    s = load_settings({"IGRIS_MULTIAGENT_DIR": "/tmp/ma"})
    assert s.multiagent_dir == Path("/tmp/ma")


def test_empty_multiagent_dir_is_none():
    assert load_settings({"IGRIS_MULTIAGENT_DIR": "  "}).multiagent_dir is None


def test_empty_workspace_is_none():
    assert load_settings({"IGRIS_WORKSPACE": ""}).workspace is None


@pytest.mark.parametrize("bad", ["", "a b", "x/y", "a" * 41, "é"])
def test_persona_validation(bad):
    with pytest.raises(ValueError, match="IGRIS_PERSONA"):
        load_settings({"IGRIS_PERSONA": bad})


def test_api_backend_defaults():
    s = load_settings({})
    assert s.backends == ("claude", "codex")
    assert s.slow_backends == ("claude", "codex")
    assert s.openai_model == "gpt-4o"
    assert s.openai_daily_budget_usd == 1.0
    assert s.budget_path == s.data_dir / "budget.json"
    assert s.gemini_model == "gemini-3.8-flash"
    assert s.ollama_model == "qwen3:4b"
    assert s.ollama_url == "http://localhost:11434"


def test_api_backend_overrides():
    s = load_settings({
        "IGRIS_BACKENDS": "openai,gemini,ollama",
        "IGRIS_SLOW_BACKENDS": "Claude, openai",
        "IGRIS_OPENAI_MODEL": "gpt-4o-mini",
        "IGRIS_OPENAI_DAILY_BUDGET_USD": "2.5",
        "IGRIS_BUDGET_PATH": "/tmp/b.json",
        "IGRIS_GEMINI_MODEL": "g",
        "IGRIS_OLLAMA_MODEL": "o",
        "IGRIS_OLLAMA_URL": "http://h:1",
    })
    assert s.backends == ("openai", "gemini", "ollama")
    assert s.slow_backends == ("claude", "openai")
    assert s.openai_model == "gpt-4o-mini"
    assert s.openai_daily_budget_usd == 2.5
    assert s.budget_path == Path("/tmp/b.json")
    assert s.gemini_model == "g"
    assert s.ollama_model == "o"
    assert s.ollama_url == "http://h:1"


@pytest.mark.parametrize("bad", ["foo", "claude,bogus", "", " , "])
def test_slow_backend_validation(bad):
    with pytest.raises(ValueError, match="IGRIS_SLOW_BACKENDS"):
        load_settings({"IGRIS_SLOW_BACKENDS": bad})


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "nan", "inf"])
def test_budget_validation(bad):
    with pytest.raises(ValueError, match="IGRIS_OPENAI_DAILY_BUDGET_USD"):
        load_settings({"IGRIS_OPENAI_DAILY_BUDGET_USD": bad})


def test_dotenv_validation_error(tmp_path):
    f = tmp_path / ".env"
    f.write_text("IGRIS_TIMEOUT=0\n")
    with pytest.raises(ValueError, match="IGRIS_TIMEOUT"):
        load_settings({}, f)


def test_ma_settings_defaults_and_override():
    s = load_settings({})
    assert s.ma_parallel == 1
    assert s.jobs_path == s.data_dir / "jobs.json"
    assert load_settings({"IGRIS_MA_PARALLEL": "2"}).ma_parallel == 2
    assert load_settings({"IGRIS_JOBS_PATH": "x/j.json"}).jobs_path == Path("x/j.json")


@pytest.mark.parametrize("raw", ["0", "5", "abc"])
def test_ma_parallel_out_of_range(raw):
    with pytest.raises(ValueError, match="IGRIS_MA_PARALLEL"):
        load_settings({"IGRIS_MA_PARALLEL": raw})


COMPAT = ("openrouter", "groq", "cerebras", "mistral", "nvidia", "cloudflare",
          "sambanova", "cohere")


def test_compat_backend_names_match_the_provider_registry():
    from igris.config import COMPAT_BACKENDS
    from igris.llm_compat import PROVIDERS

    assert set(COMPAT_BACKENDS) == set(PROVIDERS) == set(COMPAT)


@pytest.mark.parametrize("name", COMPAT)
def test_each_compat_backend_is_accepted_with_model_override(name):
    var = "IGRIS_" + name.upper() + "_MODEL"
    s = load_settings({"IGRIS_BACKENDS": "echo," + name, var: "custom/model"})
    assert s.backends == ("echo", name)
    assert s.compat_models == {name: "custom/model"}
    assert load_settings({"IGRIS_SLOW_BACKENDS": name}).slow_backends == (name,)
    assert load_settings({"IGRIS_USER_BACKENDS": name}).user_backends == (name,)


def test_compat_models_default_empty_and_blank_override_rejected():
    assert load_settings({}).compat_models == {}
    with pytest.raises(ValueError, match="IGRIS_GROQ_MODEL"):
        load_settings({"IGRIS_GROQ_MODEL": "  "})


def test_huggingface_is_not_a_backend():
    with pytest.raises(ValueError):
        load_settings({"IGRIS_BACKENDS": "huggingface"})


def test_find_multiagent_dir_next_to_the_checkout(tmp_path):
    from igris.config import find_multiagent_dir

    igris_root = tmp_path / "igris"
    igris_root.mkdir()
    assert find_multiagent_dir(igris_root) is None
    (tmp_path / "multiagent").mkdir()
    (tmp_path / "multiagent" / "multiagent.py").write_text("")
    assert find_multiagent_dir(igris_root) == tmp_path / "multiagent"


def test_sibling_multiagent_only_fills_an_unset_process_variable(monkeypatch, tmp_path):
    import igris.config as config

    monkeypatch.setattr(config, "find_multiagent_dir", lambda: tmp_path)
    monkeypatch.delenv("IGRIS_MULTIAGENT_DIR", raising=False)
    assert load_settings().multiagent_dir == tmp_path
    assert load_settings({}).multiagent_dir is None
    monkeypatch.setenv("IGRIS_MULTIAGENT_DIR", "")
    assert load_settings().multiagent_dir is None


def test_upload_limits_default_and_override():
    s = load_settings({})
    assert (s.file_max_mb, s.file_quota_mb, s.file_max_count) == (25, 500, 500)
    s = load_settings({"IGRIS_FILE_MAX_MB": "100", "IGRIS_FILE_QUOTA_MB": "2000",
                       "IGRIS_FILE_MAX_COUNT": "50"})
    assert (s.file_max_mb, s.file_quota_mb, s.file_max_count) == (100, 2000, 50)


@pytest.mark.parametrize("name,value", [("IGRIS_FILE_MAX_MB", "0"), ("IGRIS_FILE_MAX_MB", "201"),
                                        ("IGRIS_FILE_QUOTA_MB", "x"),
                                        ("IGRIS_FILE_MAX_COUNT", "10001")])
def test_bad_upload_limits(name, value):
    with pytest.raises(ValueError):
        load_settings({name: value})


STATE_NAMES = {
    "memory_path": "memory.jsonl",
    "sessions_dir": "sessions",
    "personas_dir": "personas",
    "budget_path": "budget.json",
    "jobs_path": "jobs.json",
}


def state_paths(settings):
    return {name: getattr(settings, name) for name in STATE_NAMES}


def under(root):
    return {name: root / child for name, child in STATE_NAMES.items()}


def test_all_state_lives_under_the_home_data_dir_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("IGRIS_DATA_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    settings = load_settings({})

    assert settings.data_dir == tmp_path / ".igris"
    assert state_paths(settings) == under(tmp_path / ".igris")


def test_data_dir_moves_all_state(tmp_path):
    settings = load_settings({"IGRIS_DATA_DIR": str(tmp_path)})

    assert settings.data_dir == tmp_path
    assert state_paths(settings) == under(tmp_path)


def test_data_dir_from_dotenv_moves_all_state(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"IGRIS_DATA_DIR={tmp_path / 'state'}", encoding="utf-8")

    settings = load_settings({}, dotenv=dotenv)

    assert state_paths(settings) == under(tmp_path / "state")


def test_a_specific_path_beats_the_data_dir(tmp_path):
    settings = load_settings({"IGRIS_DATA_DIR": str(tmp_path), "IGRIS_JOBS_PATH": "x/j.json"})

    assert settings.jobs_path == Path("x/j.json")
    assert settings.sessions_dir == tmp_path / "sessions"


def test_settings_built_directly_follow_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))

    assert state_paths(Settings()) == under(tmp_path)


def test_the_suite_never_points_at_the_real_data_dir():
    assert not Settings().data_dir.is_relative_to(Path.home() / ".igris")


def test_host_defaults_to_loopback():
    assert load_settings({}).host == "127.0.0.1"


def test_host_comes_from_the_environment():
    assert load_settings({"IGRIS_HOST": " 100.64.0.7 "}).host == "100.64.0.7"


def test_empty_host_is_refused():
    with pytest.raises(ValueError, match="IGRIS_HOST"):
        load_settings({"IGRIS_HOST": "  "})


def test_inbox_settings_defaults_and_bounds():
    defaults = load_settings({})
    assert defaults.inbox_interval == 300 and defaults.digest_hour == 19
    custom = load_settings({"IGRIS_INBOX_INTERVAL": "120", "IGRIS_DIGEST_HOUR": "0"})
    assert custom.inbox_interval == 120 and custom.digest_hour == 0
    for env in ({"IGRIS_INBOX_INTERVAL": "30"}, {"IGRIS_INBOX_INTERVAL": "4000"},
                {"IGRIS_DIGEST_HOUR": "24"}, {"IGRIS_DIGEST_HOUR": "-1"}):
        with pytest.raises(ValueError):
            load_settings(env)


def test_morning_hour_defaults_to_eight_and_an_empty_or_invalid_value_turns_it_off():
    assert load_settings({}).morning_hour == 8
    assert load_settings({"IGRIS_MORNING_HOUR": " 0 "}).morning_hour == 0
    assert load_settings({"IGRIS_MORNING_HOUR": "23"}).morning_hour == 23
    for raw in ("", "off", "24", "-1", "8.5", "sabah"):
        assert load_settings({"IGRIS_MORNING_HOUR": raw}).morning_hour is None


def test_esn_calendar_ids_are_a_clean_comma_list():
    assert load_settings({}).esn_calendar_ids == ()
    ids = load_settings({"IGRIS_ESN_CALENDAR_ID": " a@group.calendar.google.com, b#x@group , ,a@group.calendar.google.com"})
    assert ids.esn_calendar_ids == ("a@group.calendar.google.com", "b#x@group")
    for bad in ("a b@group", ",".join(f"c{i}" for i in range(6)), "x" * 257):
        with pytest.raises(ValueError):
            load_settings({"IGRIS_ESN_CALENDAR_ID": bad})


def test_mail_digest_and_school_domains():
    defaults = load_settings({})
    assert defaults.mail_digest is False and defaults.school_domains == ()
    custom = load_settings({"IGRIS_MAIL_DIGEST": "1",
                            "IGRIS_SCHOOL_DOMAINS": " Stud.Example.EDU, @example.edu ,, "})
    assert custom.mail_digest is True and custom.school_domains == ("stud.example.edu", "example.edu")
    for env in ({"IGRIS_MAIL_DIGEST": "maybe"}, {"IGRIS_SCHOOL_DOMAINS": "vu"},
                {"IGRIS_SCHOOL_DOMAINS": "a@b.lt"}):
        with pytest.raises(ValueError):
            load_settings(env)
def test_mail_settings_defaults_and_env():
    from igris.config import load_settings
    settings = load_settings({})
    assert (settings.mail_max_recipients, settings.mail_daily_limit,
            settings.mail_followup_days, settings.school_address) == (5, 20, 3, '')
    settings = load_settings({'IGRIS_MAIL_MAX_RECIPIENTS': '7', 'IGRIS_MAIL_DAILY_LIMIT': '30',
                              'IGRIS_MAIL_FOLLOWUP_DAYS': '4',
                              'IGRIS_SCHOOL_ADDRESS': ' owner@school.example '})
    assert (settings.mail_max_recipients, settings.mail_daily_limit,
            settings.mail_followup_days, settings.school_address) == (7, 30, 4, 'owner@school.example')


def test_mail_settings_reject_out_of_range():
    import pytest
    from igris.config import load_settings
    for name, ceiling in [('MAX_RECIPIENTS', 20), ('DAILY_LIMIT', 200), ('FOLLOWUP_DAYS', 30)]:
        for value in ['0', '-1', str(ceiling + 1), 'abc', '1.5']:
            with pytest.raises(ValueError):
                load_settings({'IGRIS_MAIL_' + name: value})


def test_core_study_and_vision_settings():
    from igris.config import load_settings

    defaults = load_settings({})
    assert defaults.core is True and defaults.study is True
    assert defaults.vision_model == defaults.web_model
    settings = load_settings({"IGRIS_CORE": "off", "IGRIS_STUDY": "0", "IGRIS_WEB_MODEL": "opus"})
    assert not settings.core and not settings.study and settings.vision_model == "opus"
    assert load_settings({"IGRIS_VISION_MODEL": "sonnet"}).vision_model == "sonnet"
    for key in ("IGRIS_CORE", "IGRIS_STUDY", "IGRIS_VISION_MODEL"):
        with pytest.raises(ValueError):
            load_settings({key: "invalid model!"})


def test_study_fast_model_defaults_override_and_empty():
    assert Settings().study_fast_model == "haiku"
    assert load_settings({"IGRIS_WEB_MODEL": "opus"}).study_fast_model == "haiku"
    assert load_settings({"IGRIS_STUDY_FAST_MODEL": " sonnet "}).study_fast_model == "sonnet"
    assert load_settings({"IGRIS_STUDY_FAST_MODEL": " "}).study_fast_model == ""
    for model in ("invalid model!", "x" * 81, "haiku\n--tools Bash"):
        with pytest.raises(ValueError, match="IGRIS_STUDY_FAST_MODEL"):
            load_settings({"IGRIS_STUDY_FAST_MODEL": model})


def test_guest_username_default_and_override():
    assert load_settings(env={}).guest_username == "guest"
    assert load_settings(env={"IGRIS_GUEST_USERNAME": " Friend "}).guest_username == "friend"
    for bad in ("", "admin", "two words", "x" * 33):
        with pytest.raises(ValueError, match="IGRIS_GUEST_USERNAME"):
            load_settings(env={"IGRIS_GUEST_USERNAME": bad})


def test_owner_context_setting():
    import pytest
    from igris.config import load_settings
    assert load_settings({}).owner_context == ''
    settings = load_settings({'IGRIS_OWNER_CONTEXT': '  The owner is\n a nurse.  '})
    assert settings.owner_context == 'The owner is a nurse.'
    with pytest.raises(ValueError):
        load_settings({'IGRIS_OWNER_CONTEXT': 'x' * 501})
