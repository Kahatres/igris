import copy
import json

import pytest

from igris import roadmap
from igris.roadmap import RoadmapError

GOOD = {
    "updated": "2025-01-02",
    "releases": [
        {"id": "r1", "title": "One", "date": "2025-01-01", "status": "done",
         "built_by": "me", "items": ["a", "b"]},
        {"id": "r2", "title": "Two", "date": "2024-12-01", "status": "done",
         "items": []},
    ],
    "plans": [
        {"id": "p1", "title": "P", "status": "in_progress", "items": ["x"]},
        {"id": "p2", "title": "Q", "status": "planned", "items": []},
        {"id": "p3", "title": "R", "status": "idea", "items": []},
    ],
}


def write(tmp_path, data):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_valid_loads_and_summary(tmp_path):
    data = roadmap.load(write(tmp_path, GOOD))
    assert data == GOOD
    assert roadmap.summary(data) == {"done": 2, "in_progress": 1, "planned": 1, "idea": 1}


def test_validate_returns_same_object():
    assert roadmap.validate(GOOD) is GOOD


def test_missing_file(tmp_path):
    with pytest.raises(RoadmapError):
        roadmap.load(tmp_path / "nope.json")


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(RoadmapError):
        roadmap.load(p)


def test_not_utf8(tmp_path):
    p = tmp_path / "bin.json"
    p.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RoadmapError):
        roadmap.load(p)


def mutate(fn):
    d = copy.deepcopy(GOOD)
    fn(d)
    return d


CASES = [
    (lambda d: d.__setitem__("updated", "2025-13-01"), "updated"),
    (lambda d: d.__setitem__("updated", 5), "updated"),
    (lambda d: d.pop("plans"), "plans"),
    (lambda d: d.__setitem__("releases", {}), "releases"),
    (lambda d: d["releases"][1].__setitem__("date", "yesterday"), "releases[1].date"),
    (lambda d: d["releases"][1].pop("date"), "releases[1].date"),
    (lambda d: d["releases"][0].__setitem__("status", "planned"), "releases[0].status"),
    (lambda d: d["plans"][2].__setitem__("status", "done"), "plans[2].status"),
    (lambda d: d["plans"][0].__setitem__("id", "Bad_ID"), "plans[0].id"),
    (lambda d: d["plans"][0].__setitem__("id", "a" * 41), "plans[0].id"),
    (lambda d: d["plans"][0].__setitem__("id", "r1"), "plans[0].id"),
    (lambda d: d["releases"][0].__setitem__("title", " "), "releases[0].title"),
    (lambda d: d["plans"][1].__setitem__("items", "x"), "plans[1].items"),
    (lambda d: d["plans"][0]["items"].append(""), "plans[0].items[1]"),
    (lambda d: d["releases"][0].__setitem__("built_by", ""), "releases[0].built_by"),
    (lambda d: d["releases"].append("x"), "releases[2]"),
]


@pytest.mark.parametrize("fn,name", CASES)
def test_invalid_names_field(fn, name):
    with pytest.raises(RoadmapError) as ei:
        roadmap.validate(mutate(fn))
    assert name in str(ei.value)


def test_top_level_not_object():
    with pytest.raises(RoadmapError):
        roadmap.validate([])


def test_real_roadmap():
    data = roadmap.load()
    assert roadmap.default_path().name == "roadmap.json"
    assert roadmap.summary(data)["done"] >= 1


def test_retired_browser_test_plan_is_closed_in_history():
    data = roadmap.load()
    assert all(plan["id"] != "gpt-arayuz-testi" for plan in data["plans"])
    closed = next(item for item in data["releases"] if item["id"] == "gpt-arayuz-testi")
    assert closed["status"] == "done"
    assert closed["date"] == "2026-11-14"
    assert "Kapatıldı" in closed["items"][0]
    assert "betikleri silindi" in closed["items"][0]
