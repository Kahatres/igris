"""Checks require agreement between the model and independent calculation."""

import json

import pytest

from igris.study_check import check
from igris import symcheck


_available = symcheck.available


@pytest.fixture(autouse=True)
def legacy_backend(monkeypatch):
    """Keep the original fallback contract tested even on SymPy installations."""
    monkeypatch.setattr(symcheck, "available", lambda: False)


@pytest.fixture
def symbolic_backend(legacy_backend, monkeypatch):
    pytest.importorskip("sympy")
    monkeypatch.setattr(symcheck, "available", _available)


PROBLEM = {"statement": "Find the dot product", "answer": {"op": "dot", "args": ["(1,2)", "(3,4)"]}}


def model(line=None, final="11", **fields):
    return lambda prompt: json.dumps(dict({"first_wrong_line": line, "reason": "Arithmetic",
                                          "should_be": "11", "final_answer": final}, **fields))


def test_right_is_verified_and_prompt_numbers_lines():
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return model()(prompt)

    result = check(PROBLEM, ["a dot b = 3 + 8", "=11"], ask_model=ask)
    assert result["verdict"] == "right" and result["verified"] is True
    assert result["line"] is None
    assert "1:" in prompts[0] and "2:" in prompts[0] and "first_wrong_line" in prompts[0]


def test_wrong_agrees_with_calculation():
    result = check(PROBLEM, ["3+8=10"], ask_model=model(1, "10"))
    assert result == {"verdict": "wrong", "line": 1, "reason": "Arithmetic",
                      "should_be": "11", "verified": True}


@pytest.mark.parametrize("line, final, claim", [(None, "10", "10"), (1, "11", "11"),
                                                 (None, "11", "10")])
def test_disagreements_are_unsure(line, final, claim):
    result = check(PROBLEM, [claim], ask_model=model(line, final))
    assert result["verdict"] == "unsure" and result["verified"] is False


@pytest.mark.parametrize("response", [
    "bad json", "```json\n{}\n```", "[]", "{}",
    '{"first_wrong_line": 2, "reason": "x", "should_be": "11", "final_answer": "11"}',
    '{"first_wrong_line": 0, "reason": "x", "should_be": "11", "final_answer": "11"}',
    '{"first_wrong_line": true, "reason": "x", "should_be": "11", "final_answer": "11"}',
    '{"first_wrong_line": null, "reason": 2, "should_be": "11", "final_answer": "11"}',
    '{"first_wrong_line":null,"first_wrong_line":1,"reason":"x","should_be":"11","final_answer":"11"}',
])
def test_malformed_or_out_of_range_json_is_unsure(response):
    result = check(PROBLEM, ["11"], ask_model=lambda _: response)
    assert result["verdict"] == "unsure" and result["line"] is None and not result["verified"]


def test_no_calculation_is_explicitly_unverified():
    result = check({"statement": "Explain a theorem"}, ["My proof"], ask_model=model(final="My proof"))
    assert result["verdict"] == "right" and not result["verified"]
    assert "model" in result["reason"].lower()


def test_calculation_failure_model_failure_and_empty_work_are_unsure():
    def failing(_):
        raise RuntimeError("Offline")

    assert check(PROBLEM, ["11"], ask_model=failing)["verdict"] == "unsure"
    assert check(PROBLEM, [], ask_model=failing)["verdict"] == "unsure"
    broken = dict(PROBLEM, answer={"op": "unknown", "args": []})
    assert check(broken, ["11"], ask_model=model())["verdict"] == "unsure"


def test_fraction_and_rounded_non_integer_answers():
    projection = {"answer": {"op": "projection", "args": ["(1,0)", "(1,2)"]}}
    claim = r"(\frac{1}{5}, \frac{2}{5})"
    assert check(projection, [claim], ask_model=model(final=claim))["verdict"] == "right"
    angle = {"answer": {"op": "angle_deg", "args": ["(1,2)", "(3,4)"]}}
    assert check(angle, ["10.3048"], ask_model=model(final="10.3048"))["verdict"] == "right"


def test_symbolic_middle_sign_error_without_model_help(symbolic_backend):
    def no_model(_):
        pytest.fail("A certain calculation does not need model help")

    lines = ["(x-1)^2=x^2-2*x+1", "x^2-2*x+1=x^2+2*x+1", "1+1=2"]
    result = check({"statement": "Expand"}, lines, ask_model=no_model)
    assert result["verdict"] == "wrong" and result["line"] == 2
    assert result["verified"] and not result["model_consulted"]


def test_symbolic_unusual_valid_step_is_right(symbolic_backend):
    lines = ["(x+1)^2=x^2+2*x+1", "x^2+2*x+1=1+x*(x+2)"]
    result = check({}, lines, ask_model=model(final=lines[-1]))
    assert result["verdict"] == "right" and result["verified"]
    assert not result["partial_verification"]


def test_symbolic_model_cannot_mark_correct_identity_wrong(symbolic_backend):
    result = check({}, ["(x+1)^2=1+x*(x+2)"], ask_model=model(1))
    assert result["verdict"] == "unsure"
    assert "model" in result["reason"].lower()
    assert not result["verified"]


def test_symbolic_unreadable_lines_remain_explicit_model_reading(symbolic_backend):
    result = check({}, ["1+1=2", "By the theorem the claim follows"], ask_model=model())
    assert result["verdict"] == "right" and not result["verified"]
    assert result["partial_verification"]
    assert result["line_checks"][1]["status"] == "unreadable"


def test_symbolic_uncertain_first_line_disagreement_is_unsure(symbolic_backend):
    result = check({}, ["By the theorem", "2+3=6"], ask_model=model(1))
    assert result["verdict"] == "unsure" and result["line"] is None
    assert result["partial_verification"] and not result["verified"]
    assert "disagree" in result["reason"]


def test_symbolic_certain_calculation_overrides_bad_model_opinion(symbolic_backend):
    result = check({}, ["1+1=2", "2+3=6"], ask_model=model(1))
    assert result["verdict"] == "wrong" and result["line"] == 2
    assert result["verified"]


def test_symbolic_certain_error_with_unreadable_later_line(symbolic_backend):
    result = check({}, ["1+1=3", "Explanation"], ask_model=model())
    assert result["line"] == 1 and result["verdict"] == "wrong"
    assert result["partial_verification"]


def test_symbolic_missing_model_is_unsure_for_all_correct_lines(symbolic_backend):
    def failing(_):
        raise RuntimeError("Unavailable")

    result = check({}, ["1+1=2"], ask_model=failing)
    assert result["verdict"] == "unsure"


def test_symbolic_inconclusive_calculation_keeps_model_reading(monkeypatch, symbolic_backend):
    monkeypatch.setattr(symcheck, "verify_lines", lambda _: [{"status": "unknown", "line": 1}])
    result = check({}, ["x=x"], ask_model=model())
    assert result["verdict"] == "right" and not result["verified"]
    assert result["partial_verification"]


def test_symbolic_unreadable_error_stays_model_only(symbolic_backend):
    result = check({}, ["A theorem claim"], ask_model=model(1))
    assert result["verdict"] == "wrong" and result["line"] == 1
    assert result["partial_verification"] and not result["verified"]


def test_symbolic_model_and_partial_calculation_agree(symbolic_backend):
    result = check({}, ["A premise", "2+3=6"], ask_model=model(2))
    assert result["verdict"] == "wrong" and result["line"] == 2
    assert result["partial_verification"] and not result["verified"]


def test_symbolic_solution_equations_remain_model_only(symbolic_backend):
    result = check({"statement": "Solve x^2=4 for positive x"},
                   ["x^2=4", "x=2"], ask_model=model(final="2"))
    assert result["verdict"] == "right" and not result["verified"]
    assert result["partial_verification"]


def test_fully_calculated_solution_is_right_without_the_model(symbolic_backend):
    import pytest
    pytest.importorskip("sympy")
    from igris import study_check

    problem = {"statement": "a.b", "answer": {"op": "dot", "args": ["(1,2,3)", "(4,-5,6)"]}}

    def no_model(prompt):
        raise AssertionError("the model must not be consulted")

    result = study_check.check(problem, ["4-10+18=4+8", "4+8=12"], ask_model=no_model)
    assert result["verdict"] == "right" and result["verified"] and not result["model_consulted"]
    wrong_end = study_check.check(problem, ["4-10+18=4+8", "4+8=13"], ask_model=no_model)
    assert wrong_end["verdict"] == "wrong" and wrong_end["line"] == 2
