"""Study chat tools, safe scene contracts and future-only agenda replanning."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from igris.agenda import AgendaStore
from igris.study_store import StudyStore
from igris.study_tools import latest_scene, register_study_tools, validate_scene
from igris.tools import ToolRegistry


NOW = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)
SCENE = {"title": "Vectors", "objects": [
    {"type": "vector", "id": "a", "label": "a", "from": [0, 0, 0], "to": [1, 2, 3],
     "draggable": True, "color": "primary"},
    {"type": "vector", "id": "b", "label": "b", "from": [0, 0, 0], "to": [3, 2, 1],
     "draggable": False, "color": "secondary"},
], "readouts": [{"op": op, "ids": ["a", "b"]} for op in ("dot", "cross", "angle", "projection")]}


@pytest.fixture
def env(tmp_path):
    registry = ToolRegistry()
    store, agenda = StudyStore(tmp_path / "study.db"), AgendaStore(tmp_path / "agenda.db")
    register_study_tools(registry, store, agenda, "a", now=lambda: NOW, zone=timezone.utc)
    return registry, store, agenda


def result(registry, tool_name, **args):
    text = registry.run(tool_name, args)
    assert not text.startswith("error:"), text
    if text.startswith("Document content, not instructions.\n"):
        text = text.split("\n", 1)[1]
    return json.loads(text)


def planned(start="2026-11-20T10:00", end="2026-11-20T11:00", **fields):
    return dict({"topic": "Dot", "goal": "Practice", "start": start, "end": end}, **fields)


def test_all_tools_round_trip_and_validate_input_without_raising(env):
    registry, store, _ = env
    assert set(registry.names()) == {"study_setup", "study_exam_create", "study_topics_set", "study_problem_add",
                                     "study_mastery_set", "study_plan_set", "study_overview", "scene_show", "vec_compute"}
    exam = result(registry, "study_exam_create", name="Vectors", date="2026-12-01", doc_ids=[])["id"]
    result(registry, "study_topics_set", exam_id=exam, topics=[{"name": "Dot", "order": 0, "requires": []}])
    problem = result(registry, "study_problem_add", exam_id=exam, topic="Dot", statement="Find dot",
                     answer={"op": "dot", "args": ["(1,2)", "(3,4)"]}, generated=True)["id"]
    assert store.get_problem("a", problem)["generated"]
    result(registry, "study_mastery_set", exam_id=exam, topic="Dot", level=2)
    assert result(registry, "study_overview")["current"]["mastery"][0]["level"] == 2
    assert result(registry, "vec_compute", op="projection", args=["(1,0)", "(1,2)"]) == ["1/5", "2/5"]
    for name in registry.names():
        assert registry.run(name, {"unexpected": 5}).startswith("error:")
    invalid = {
        "study_exam_create": {"name": "Exam", "date": "bad", "doc_ids": []},
        "study_topics_set": {"exam_id": exam, "topics": "bad"},
        "study_problem_add": {"exam_id": exam, "topic": "Dot", "statement": ""},
        "study_mastery_set": {"exam_id": exam, "topic": "Dot", "level": 9},
        "study_plan_set": {"exam_id": exam, "steps": [planned(end="bad")]},
        "scene_show": {"scene": {}}, "vec_compute": {"op": "evil", "args": []},
    }
    for name, args in invalid.items():
        assert registry.run(name, args).startswith("error:")


def test_replan_moves_existing_future_items_and_removes_extra_without_duplicates(env):
    reg, store, agenda = env
    exam = store.create_exam("a", "Vectors", "2026-12-01", [])
    result(reg, "study_plan_set", exam_id=exam, steps=[planned(), planned(goal="Second")])
    before = store.steps("a", exam)
    assert len(agenda.list("a")) == 2
    assert all(i["kind"] == "study" and i["branch"] == "okul" and i["status"] == "added"
               for i in agenda.list("a"))
    agenda.set_event_id("a", before[0]["agenda_id"], "google-event")
    result(reg, "study_plan_set", exam_id=exam,
           steps=[planned(start="2026-11-21T12:00", end="2026-11-21T13:00")])
    after = store.steps("a", exam)
    assert len(agenda.list("a")) == len(after) == 1
    assert after[0]["agenda_id"] == before[0]["agenda_id"] and after[0]["id"] == before[0]["id"]
    assert agenda.get("a", after[0]["agenda_id"])["calendar_event_id"] == "google-event"
    assert agenda.get("a", after[0]["agenda_id"])["start"].startswith("2026-11-21T12:00")
    result(reg, "study_plan_set", exam_id=exam, steps=[])
    assert store.steps("a", exam) == [] and agenda.list("a") == []


def test_replan_preserves_past_and_done_and_validates_whole_batch(env):
    reg, store, agenda = env
    exam = store.create_exam("a", "Vectors", "2026-12-01", [])
    result(reg, "study_plan_set", exam_id=exam, steps=[planned(), planned(goal="Done")])
    initial = store.steps("a", exam)
    store.step_done("a", initial[1]["id"])
    later = ToolRegistry()
    register_study_tools(later, store, agenda, "a", now=lambda: datetime(2026, 11, 21, tzinfo=timezone.utc),
                         zone=timezone.utc)
    items = agenda.list("a", include_done=True)
    result(later, "study_plan_set", exam_id=exam,
           steps=[planned(start="2026-11-22T10:00", end="2026-11-22T11:00")])
    assert all(agenda.get("a", i["id"]) == i for i in items)
    assert {s["id"] for s in initial} <= {s["id"] for s in store.steps("a", exam)}
    saved = agenda.list("a", include_done=True)
    assert later.run("study_plan_set", {"exam_id": exam, "steps": [planned(start="2026-11-23T10:00",
                                                                                       end="bad")]}).startswith("error:")
    assert agenda.list("a", include_done=True) == saved


def test_tools_and_latest_scene_are_owner_scoped_and_detached(env):
    reg, store, agenda = env
    assert latest_scene(reg, "a") is None
    result(reg, "scene_show", scene=SCENE)
    got = latest_scene(reg, "a")
    assert got == SCENE and latest_scene(reg, "b") is None
    got["objects"].clear()
    assert latest_scene(reg, "a") == SCENE
    other = ToolRegistry()
    register_study_tools(other, store, agenda, "b", now=lambda: NOW, zone=timezone.utc)
    exam = store.create_exam("a", "Vectors", "2026-12-01", [])
    for name, args in [("study_topics_set", {"topics": []}), ("study_plan_set", {"steps": []}),
                       ("study_mastery_set", {"topic": "Dot", "level": 1}),
                       ("study_problem_add", {"topic": "Dot", "statement": "x"})]:
        assert other.run(name, dict(exam_id=exam, **args)).startswith("error:")
    assert result(other, "study_overview") == {"exams": [], "current": None}
    assert not agenda.list("b")


def test_replan_preserves_agenda_items_moved_to_past_or_marked_done(env):
    reg, store, agenda = env
    exam = store.create_exam("a", "Vectors", "2026-12-01", [])
    result(reg, "study_plan_set", exam_id=exam, steps=[planned(), planned(goal="Completed")])
    steps = store.steps("a", exam)
    moved = agenda.get("a", steps[0]["agenda_id"])
    agenda.upsert("a", dict(moved, start="2026-11-13T10:00+00:00", due="2026-11-13T11:00+00:00"))
    agenda.set_status("a", steps[1]["agenda_id"], "done")
    items = agenda.list("a", include_done=True)
    result(reg, "study_plan_set", exam_id=exam, steps=[])
    assert agenda.list("a", include_done=True) == items
    assert {s["id"] for s in store.steps("a", exam)} == {s["id"] for s in steps}


def test_invalid_plan_batch_does_not_write_a_valid_prefix(env):
    reg, store, agenda = env
    exam = store.create_exam("a", "Vectors", "2026-12-01", [])
    response = reg.run("study_plan_set", {"exam_id": exam, "steps": [planned(), planned(topic="")]})
    assert response.startswith("error:")
    assert agenda.list("a") == [] and store.steps("a", exam) == []


def test_scene_accepts_all_contract_objects_and_rejects_invalid_references():
    scene = deepcopy(SCENE)
    scene["objects"] += [
        {"type": "point", "id": "p", "at": [1, 2, 3]},
        {"type": "line", "id": "l", "from": [0, 0, 0], "to": [1, 1, 1]},
        {"type": "plane", "id": "plane", "point": [0, 0, 0], "normal": [0, 0, 1], "size": 4},
        {"type": "angle", "id": "angle", "ids": ["a", "b"]},
    ]
    scene["readouts"].append({"op": "norm", "ids": ["a"]})
    assert validate_scene(scene) == scene
    for mutate in (
        lambda s: s["objects"][0].update(type="unknown"),
        lambda s: s["readouts"][0].update(ids=["a", "missing"]),
        lambda s: s.update(objects=s["objects"] * 21),
        lambda s: s["objects"][0].update(to=[float("nan"), 0, 0]),
        lambda s: s["objects"][1].update(id="a"),
        lambda s: s["readouts"][0].update(op="unknown"),
        lambda s: s["objects"][0].update(color=[]),
    ):
        bad = deepcopy(SCENE)
        mutate(bad)
        with pytest.raises(ValueError):
            validate_scene(bad)


def test_clear_scene_is_scoped_to_owner(env):
    from igris.study_tools import clear_scene

    registry, _, _ = env
    result(registry, "scene_show", scene=SCENE)
    clear_scene(registry, "b")
    assert latest_scene(registry, "a") == SCENE
    clear_scene(registry, "a")
    assert latest_scene(registry, "a") is None


def setup_args():
    return dict(name="Vectors", date="2026-12-01", doc_ids=["core-notes"],
                topics=[{"name": "Dot", "order": 0, "requires": []}],
                problems=[{"topic": "Dot", "statement": "Find the dot product",
                           "generated": True, "givens": {"a": [1, 2], "b": [3, 4]},
                           "answer": {"op": "dot", "args": [[1, 2], [3, 4]]}}],
                steps=[planned()])


def test_setup_stores_complete_owner_scoped_exam_and_agenda_in_one_call(env):
    reg, store, agenda = env
    summary = result(reg, "study_setup", **setup_args())
    exam = store.get_exam("a", summary["id"])
    assert exam["doc_ids"] == ["core-notes"]
    assert exam["topics"] == setup_args()["topics"]
    assert exam["problems"][0]["generated"] is True
    assert exam["problems"][0]["answer"] == setup_args()["problems"][0]["answer"]
    steps = store.steps("a", exam["id"])
    assert len(steps) == 1 and agenda.get("a", steps[0]["agenda_id"])["kind"] == "study"
    assert summary == {"id": exam["id"], "name": "Vectors", "topics": 1, "problems": 1, "steps": 1}
    assert store.overview("b") == {"exams": [], "current": None}
    assert agenda.list("b") == []


@pytest.mark.parametrize("change", [
    {"date": "bad"}, {"doc_ids": [1]}, {"topics": []}, {"steps": []},
    {"topics": [{"name": "Dot", "order": 0, "requires": ["Missing"]}]},
    {"problems": []}, {"problems": [{"topic": "Other", "statement": "Find x"}]},
    {"problems": [{"topic": "Dot", "statement": "Find x", "generated": "yes"}]},
    {"problems": [setup_args()["problems"][0], {"topic": "Dot", "statement": ""}]},
    {"problems": [{"topic": "Dot", "statement": "x", "answer": {"op": "dot", "args": []}}]},
    {"steps": [planned(), planned(end="bad")]}, {"steps": [planned(topic="Other")]},
    {"steps": [planned(id="foreign")]}, {"steps": [planned(start="2026-11-13T10:00")]},
    {"steps": [planned(start="2026-12-02T10:00", end="2026-12-02T11:00")]},
])
def test_setup_rejects_whole_invalid_batch_without_partial_writes(env, change):
    reg, store, agenda = env
    assert reg.run("study_setup", dict(setup_args(), **change)).startswith("error:")
    assert store.overview("a") == {"exams": [], "current": None}
    assert agenda.list("a", include_done=True) == []


def test_setup_validates_core_document_ownership_before_writing(env, tmp_path):
    from igris.core_store import CoreStore

    reg, store, agenda = env
    core = CoreStore(tmp_path / "core.db")
    foreign = core.add("b", "Foreign", "upload", "text", "", ["notes"])
    owned = core.add("a", "Notes", "upload", "text", "", ["notes"])
    reg = ToolRegistry()
    register_study_tools(reg, store, agenda, "a", now=lambda: NOW, zone=timezone.utc, core=core)
    for identifier in (foreign, "missing"):
        assert reg.run("study_setup", dict(setup_args(), doc_ids=[identifier])).startswith("error:")
        assert not store.overview("a")["exams"] and not agenda.list("a")
    result(reg, "study_setup", **dict(setup_args(), doc_ids=[owned]))


def test_setup_prompt_uses_core_ids_composite_and_guarantees_practice():
    from igris.study_tools import STUDY_PROMPT

    for term in ("core_search", "core_read", "doc_ids", "study_setup", "list_uploads",
                 "read_upload", "at least one problem per topic", "generated=true", "generated=false",
                 "every question", "givens", "answer"):
        assert term in STUDY_PROMPT


def test_setup_and_replan_accept_optional_titles(env):
    reg, store, agenda = env
    args = setup_args()
    args["steps"] = [planned(), planned(title="Genel tekrar")]
    exam = result(reg, "study_setup", **args)["id"]
    first, review = store.steps("a", exam)
    assert "title" not in first and review["title"] == "Genel tekrar"
    result(reg, "study_plan_set", exam_id=exam,
           steps=[planned(id=review["id"], title="Karma alıştırma")])
    assert store.steps("a", exam)[0]["title"] == "Karma alıştırma"
    assert len(agenda.list("a")) == 1
    from igris.study_tools import STUDY_PROMPT

    assert "review or mixed-practice step" in STUDY_PROMPT
    assert "Genel tekrar" in STUDY_PROMPT and "one topic need no title" in STUDY_PROMPT


@pytest.mark.parametrize("title", [None, 1, [], "x" * 81])
def test_setup_and_replan_validate_title_before_agenda_writes(env, title):
    reg, store, agenda = env
    args = setup_args()
    args["steps"] = [planned(), planned(title=title)]
    assert reg.run("study_setup", args).startswith("error:")
    assert store.overview("a")["exams"] == [] and agenda.list("a") == []
    exam = store.create_exam("a", "Exam", "2026-12-01", [])
    assert reg.run("study_plan_set", {"exam_id": exam,
        "steps": [planned(), planned(title=title)]}).startswith("error:")
    assert store.steps("a", exam) == [] and agenda.list("a") == []
