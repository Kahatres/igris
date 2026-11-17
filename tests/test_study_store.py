"""Persistence, owner boundaries and study progress accounting."""

import pytest

from igris.study_store import StudyStore


@pytest.fixture
def store(tmp_path):
    return StudyStore(tmp_path / "study.sqlite3")


def setup_exam(store, owner="a"):
    exam = store.create_exam(owner, "Vectors", "2026-12-01", ["doc1"])
    store.set_topics(owner, exam, [
        {"name": "Basics", "order": 0, "requires": []},
        {"name": "Dot", "order": 1, "requires": ["Basics"]},
    ])
    return exam


def step(topic="Dot", **fields):
    return dict({"topic": topic, "goal": "Practice", "start": "2026-11-20T10:00+02:00",
                 "end": "2026-11-20T11:00+02:00", "agenda_id": "ag1"}, **fields)


def test_round_trip_and_reopen(store):
    exam = setup_exam(store)
    problem = store.add_problem("a", exam, "Dot", "Find dot", givens={"a": [1, 2]},
                                answer={"op": "dot", "args": ["(1,2)", "(3,4)"]}, generated=True)
    store.set_steps("a", exam, [step()])
    store.set_mastery("a", exam, "Dot", 2)
    attempt = store.record_attempt("a", problem, ["11"], "right", None, "Correct")
    reopened = StudyStore(store.path)
    current = reopened.overview("a")["current"]
    assert current["exam"]["id"] == exam
    assert current["exam"]["doc_ids"] == ["doc1"]
    assert [t["name"] for t in current["exam"]["topics"]] == ["Basics", "Dot"]
    saved = reopened.get_problem("a", problem)
    assert saved["generated"] is True and saved["givens"] == {"a": [1, 2]}
    assert saved["answer"]["op"] == "dot"
    assert reopened.get_attempt("a", attempt)["lines"] == ["11"]
    assert reopened.attempts("a", problem)[0]["id"] == attempt
    assert current["steps"][0]["agenda_id"] == "ag1"
    assert next(m for m in current["mastery"] if m["topic"] == "Dot")["level"] == 2


def test_set_steps_preserves_done_and_counts_mistakes_and_corrections(store):
    exam = setup_exam(store)
    problem = store.add_problem("a", exam, "Dot", "Find dot")
    store.set_steps("a", exam, [step(), step("Basics", agenda_id="ag2")])
    done = store.overview("a")["current"]["steps"][0]["id"]
    store.step_done("a", done)
    store.set_steps("a", exam, [step(goal="New plan")])
    steps = store.overview("a")["current"]["steps"]
    assert len(steps) == 2 and steps[0]["id"] == done and steps[0]["state"] == "done"
    wrong = store.record_attempt("a", problem, ["5"], "wrong", 1, "Arithmetic")
    store.record_attempt("a", problem, ["6"], "unsure", None, "Unclear")
    store.record_attempt("a", problem, ["11"], "right", None, "Correct")
    store.mark_corrected("a", wrong)
    store.mark_corrected("a", wrong)
    mastery = next(m for m in store.overview("a")["current"]["mastery"] if m["topic"] == "Dot")
    assert mastery == {"topic": "Dot", "level": 0, "attempts": 3, "mistakes": 1, "corrected": 1}


def test_every_method_is_owner_scoped(store):
    exam = setup_exam(store)
    problem = store.add_problem("a", exam, "Dot", "Find dot")
    store.set_steps("a", exam, [step()])
    sid = store.overview("a")["current"]["steps"][0]["id"]
    attempt = store.record_attempt("a", problem, ["1"], "wrong", 1, "Wrong")
    assert store.overview("b") == {"exams": [], "current": None}
    operations = [
        lambda: store.set_topics("b", exam, []),
        lambda: store.add_problem("b", exam, "Dot", "Find dot"),
        lambda: store.set_steps("b", exam, []), lambda: store.step_done("b", sid),
        lambda: store.record_attempt("b", problem, ["2"], "right", None, "Correct"),
        lambda: store.mark_corrected("b", attempt),
        lambda: store.set_mastery("b", exam, "Dot", 1),
        lambda: store.get_exam("b", exam), lambda: store.get_problem("b", problem),
        lambda: store.get_attempt("b", attempt), lambda: store.get_step("b", sid),
        lambda: store.steps("b", exam),
        lambda: store.attempts("b", problem),
    ]
    for operation in operations:
        with pytest.raises(KeyError):
            operation()
    own = setup_exam(store, "b")
    assert store.overview("b")["exams"][0]["id"] == own
    assert store.get_attempt("a", attempt)["corrected"] is False


@pytest.mark.parametrize("level", [-1, 4, True, "1"])
def test_mastery_validation(store, level):
    exam = setup_exam(store)
    with pytest.raises(ValueError):
        store.set_mastery("a", exam, "Dot", level)


def test_invalid_batches_do_not_change_saved_data(store):
    exam = setup_exam(store)
    store.set_steps("a", exam, [step()])
    with pytest.raises(ValueError):
        store.set_steps("a", exam, [step(), step(start="bad")])
    assert len(store.overview("a")["current"]["steps"]) == 1
    with pytest.raises(ValueError):
        store.set_topics("a", exam, [{"name": "Dot", "order": 1, "requires": ["Missing"]}])
    assert len(store.get_exam("a", exam)["topics"]) == 2


def test_explanations_and_verification_survive_reopen(store):
    exam = setup_exam(store)
    problem = store.add_problem("a", exam, "Dot", "Calculate")
    identifier = store.record_attempt("a", problem, ["31"], "wrong", 1, "Wrong",
                                       should_be="32", verified=True)
    saved = StudyStore(store.path).get_attempt("a", identifier)
    assert saved["should_be"] == "32" and saved["verified"] is True
    for fields in ({"should_be": 1}, {"verified": "yes"}):
        with pytest.raises(ValueError):
            store.record_attempt("a", problem, ["31"], "wrong", 1, "Wrong", **fields)


def test_step_title_round_trip_with_legacy_rows_and_replanning(store):
    exam = setup_exam(store)
    store.set_steps("a", exam, [step(), step(title="Genel tekrar")])
    legacy, review = store.steps("a", exam)
    assert "title" not in legacy
    store.step_done("a", legacy["id"])
    reopened = StudyStore(store.path)
    assert reopened.overview("a")["current"]["steps"][1]["title"] == "Genel tekrar"
    reopened.set_steps("a", exam, [dict(review, title="Karma alıştırma")])
    saved = reopened.overview("a")["current"]["steps"]
    assert saved[0]["id"] == legacy["id"] and "title" not in saved[0]
    assert reopened.get_step("a", review["id"])["title"] == "Karma alıştırma"
    reopened.set_steps("a", exam, [step(title="x" * 80)])
    assert reopened.steps("a", exam)[1]["title"] == "x" * 80


@pytest.mark.parametrize("title", [None, 1, True, [], {}, "x" * 81])
def test_invalid_step_titles_do_not_replace_existing_plan(store, title):
    exam = setup_exam(store)
    store.set_steps("a", exam, [step()])
    before = store.steps("a", exam)
    with pytest.raises(ValueError, match="title"):
        store.set_steps("a", exam, [step(), step(title=title)])
    assert store.steps("a", exam) == before
