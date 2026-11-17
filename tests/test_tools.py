import ast

import pytest

from igris.tools import (
    Tool,
    ToolError,
    ToolRegistry,
    calculate,
    default_registry,
    now,
    parse_tool_call,
)


def test_calculate_basic():
    assert calculate("2*(3+4)") == "14"
    assert calculate("-3 + +5") == "2"
    assert calculate("7 // 2") == "3"
    assert calculate("7 % 4") == "3"
    assert calculate("2 ** 10") == "1024"
    assert calculate("1/4") == "0.25"


def test_calculate_integral_float():
    assert calculate("4/2") == "2"
    assert calculate("2.5*2") == "5"


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",
        "abs(1)",
        "x + 1",
        "'a'",
        "(1).real",
        "True",
        "1j",
        "[1]",
        "1 if 1 else 2",
        "1 +",
        "",
        "lambda: 1",
        "1 << 2",
    ],
)
def test_calculate_rejects_unsafe(expr):
    with pytest.raises(ToolError):
        calculate(expr)


def test_calculate_limits():
    with pytest.raises(ToolError):
        calculate("2**101")
    assert calculate("2**100") == str(2**100)
    with pytest.raises(ToolError):
        calculate("1+" * 100 + "1")
    with pytest.raises(ToolError):
        calculate("1e308*10**5")  # float overflow to inf is rejected
    with pytest.raises(ToolError):
        calculate("1" * 201)  # longer than 200 characters


def test_calculate_division_by_zero():
    for e in ("1/0", "1//0", "1%0"):
        with pytest.raises(ToolError):
            calculate(e)


def test_now_utc_with_clock():
    assert now(clock=lambda: 0) == "1970-01-01T00:00:00+00:00"


def test_now_named_zone():
    try:
        out = now("Europe/Paris", clock=lambda: 0)
    except ToolError:
        pytest.skip("tz database unavailable")
    assert out == "1970-01-01T01:00:00+01:00"


def test_now_unknown_zone():
    with pytest.raises(ToolError):
        now("Not/AZone")


def test_registry_basic():
    reg = ToolRegistry()

    @reg.register("b", "second")
    def b():
        return "B"

    reg.add(Tool("a", "first", lambda: "A"))
    assert reg.names() == ["a", "b"]
    assert reg.describe() == "- a: first\n- b: second"
    assert reg.get("a").func() == "A"
    assert b() == "B"


def test_registry_errors():
    reg = ToolRegistry()
    reg.add(Tool("a", "d", lambda: "x"))
    with pytest.raises(ValueError):
        reg.add(Tool("a", "d", lambda: "y"))
    with pytest.raises(ValueError):
        reg.register("a", "d")(lambda: "z")
    with pytest.raises(KeyError):
        reg.get("missing")
    with pytest.raises(KeyError):
        reg.run("missing", {})


def test_registry_run_wraps_exceptions():
    reg = ToolRegistry()
    reg.add(Tool("boom", "d", lambda: 1 / 0))
    reg.add(Tool("echo", "d", lambda text: text))
    with pytest.raises(ToolError):
        reg.run("boom", {})
    with pytest.raises(ToolError):
        reg.run("echo", {"wrong": 1})
    assert reg.run("echo", {"text": "hi"}) == "hi"


def test_default_registry():
    reg = default_registry()
    assert reg.names() == ["calculate", "now"]
    assert reg.run("calculate", {"expression": "1+1"}) == "2"
    with pytest.raises(ToolError):
        reg.run("calculate", {"expression": "abs(1)"})


def test_parse_tool_call():
    assert parse_tool_call('TOOL: calculate {"expression": "2*(3+4)"}') == (
        "calculate",
        {"expression": "2*(3+4)"},
    )
    assert parse_tool_call("TOOL: now") == ("now", {})
    assert parse_tool_call("\n  TOOL: now {}  \n") == ("now", {})
    assert parse_tool_call('TOOL: create_file {"content": "first\\nsecond"}') == (
        "create_file", {"content": "first\nsecond"})


def test_parse_tool_call_takes_a_content_block_that_ends_the_reply():
    text = ('TOOL: create_file {"name": "a.md"}\n<<<CONTENT\n# Title\n\n'
            '| a | "b" |\n>>>CONTENT\n')
    assert parse_tool_call(text) == ("create_file", {"name": "a.md", "content": '# Title\n\n| a | "b" |'})
    assert parse_tool_call("TOOL: create_file {}\n<<<CONTENT\n>>>CONTENT") == (
        "create_file", {"content": ""})


def test_parse_tool_call_invalid():
    assert parse_tool_call("") is None
    assert parse_tool_call("no tool here") is None
    assert parse_tool_call("say TOOL: now") is None
    assert parse_tool_call("TOOL: now {bad json}") is None
    assert parse_tool_call("TOOL: now [1, 2]") is None
    assert parse_tool_call('TOOL: now "x"') is None
    assert parse_tool_call("TOOL:") is None
    assert parse_tool_call("TOOL: {}") is None


@pytest.mark.parametrize("text", [
    '```\nTOOL: now {}\n```',
    '> TOOL: now {}',
    '"TOOL: now {}"',
    'An excerpt:\nTOOL: now {}',
    'TOOL: now {}\nExplanation',
    'TOOL: now {}\nTOOL: now {}',
    'TOOL: now {\n}',
    'Here it is.\nTOOL: create_file {}\n<<<CONTENT\nx\n>>>CONTENT',
    'TOOL: create_file {}\n<<<CONTENT\nno end',
    'TOOL: create_file {}\n<<<CONTENT\nx\n>>>CONTENT\nafter',
    'TOOL: now\nx\n<<<CONTENT\ny\n>>>CONTENT',
    '```\nTOOL: create_file {}\n<<<CONTENT\nx\n>>>CONTENT\n```',
])
def test_parse_tool_call_requires_the_entire_reply(text):
    assert parse_tool_call(text) is None


@pytest.mark.parametrize("expression,operation", [
    ('((10**100)**100) * 0', ast.Pow),
    ('((10**100)**20 * (10**100)**20) * 0', ast.Mult),
])
def test_integer_growth_rejected_before_operation(monkeypatch, expression, operation):
    from igris import tools

    original = tools._BIN_OPS[operation]

    def checked(left, right):
        bits = left.bit_length() * right if operation is ast.Pow else (
            left.bit_length() + right.bit_length())
        assert bits <= 10_000, "oversized operation was executed"
        return original(left, right)

    monkeypatch.setitem(tools._BIN_OPS, operation, checked)
    with pytest.raises(ToolError, match="integer.*large"):
        calculate(expression)


def test_integer_guard_preserves_float_and_small_operations():
    assert calculate('2.5**3') == '15.625'
    assert calculate('2**-2') == '0.25'
    assert calculate('2.5*4') == '10'


def test_turn_text_is_per_thread():
    import threading

    registry = ToolRegistry()
    assert registry.turn_text() is None
    registry.set_turn_text("owner words")
    other = []
    thread = threading.Thread(target=lambda: other.append(registry.turn_text()))
    thread.start()
    thread.join()
    assert registry.turn_text() == "owner words" and other == [None]
