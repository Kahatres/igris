import pytest

from igris.core_store import CoreStore
from igris.core_tools import UNTRUSTED_LINE, register_core_tools
from igris.tools import ToolRegistry


def setup_tools(tmp_path, save=None):
    store = CoreStore(tmp_path / "core.db")
    registry = ToolRegistry()
    register_core_tools(registry, store, "alice", save_research=save)
    return store, registry


def assert_result(text):
    assert text.startswith(UNTRUSTED_LINE + "\n")
    assert len(text) <= 3000


def test_three_tools_and_save_callback(tmp_path):
    seen = []

    def save(title, text, urls):
        seen.append((title, text, urls))
        return "saved-id"

    _, registry = setup_tools(tmp_path, save)
    assert registry.names() == ["core_read", "core_save_research", "core_search"]
    result = registry.run("core_save_research", {"title": "Course", "text": "Answer",
                                                "urls": ["https://example.org"]})
    assert_result(result)
    assert "saved-id" in result
    assert seen == [("Course", "Answer", ["https://example.org"])]


def test_no_save_tool_without_callback(tmp_path):
    _, registry = setup_tools(tmp_path)
    assert registry.names() == ["core_read", "core_search"]


def test_search_and_read_card_and_numbered_page(tmp_path):
    store, registry = setup_tools(tmp_path)
    doc_id = store.add("alice", "Vectors", "upload", "mixed", "Topics: vector",
                       ["first", "vector photo"], from_image=[False, True])
    for args, expected in (({"doc_id": doc_id}, "Topics: vector"),
                           ({"doc_id": doc_id, "page": 1}, "first"),
                           ({"doc_id": doc_id, "page": 2}, "vector photo")):
        result = registry.run("core_read", args)
        assert_result(result)
        assert expected in result
    result = registry.run("core_search", {"query": "vector"})
    assert_result(result)
    assert doc_id in result and "Vectors" in result


def test_all_results_capped_including_metadata(tmp_path):
    store, registry = setup_tools(tmp_path, lambda *args: "s" * 5000)
    doc_id = store.add("alice", "t" * 5000, "upload", "text", "word " * 2000,
                       ["body " * 2000])
    results = [registry.run("core_search", {"query": "word"}),
               registry.run("core_read", {"doc_id": doc_id}),
               registry.run("core_read", {"doc_id": doc_id, "page": 1}),
               registry.run("core_save_research", {"title": "T", "text": "answer", "urls": []})]
    for result in results:
        assert_result(result)
        assert len(result) == 3000


@pytest.mark.parametrize("args", [{"doc_id": "missing"}, {"doc_id": "missing", "page": 1},
                                  {"doc_id": "missing", "page": -1},
                                  {"doc_id": "missing", "page": True},
                                  {"doc_id": "missing", "page": "1"}])
def test_unknown_document_and_invalid_pages_return_errors(tmp_path, args):
    _, registry = setup_tools(tmp_path)
    result = registry.run("core_read", args)
    assert_result(result)
    assert "error" in result.lower()


def test_owner_bound_tools_do_not_leak(tmp_path):
    store, registry = setup_tools(tmp_path)
    doc_id = store.add("bob", "private title", "research", "text", "secret", ["secret"])
    result = registry.run("core_read", {"doc_id": doc_id})
    assert_result(result)
    assert "error" in result.lower() and "secret" not in result
    result = registry.run("core_search", {"query": "secret"})
    assert_result(result)
    assert doc_id not in result and "private title" not in result


@pytest.mark.parametrize("args", [{"query": ""}, {"query": None}, {"query": ["word"]}])
def test_invalid_search_returns_error(tmp_path, args):
    _, registry = setup_tools(tmp_path)
    result = registry.run("core_search", args)
    assert_result(result)
    assert "error" in result.lower()


@pytest.mark.parametrize("args", [{"title": "", "text": "answer", "urls": []},
                                  {"title": "T", "text": "", "urls": []},
                                  {"title": "T", "text": "answer", "urls": "url"},
                                  {"title": "T", "text": "answer", "urls": [3]}])
def test_invalid_save_does_not_call_callback(tmp_path, args):
    _, registry = setup_tools(tmp_path, lambda *args: pytest.fail("invalid save"))
    result = registry.run("core_save_research", args)
    assert_result(result)
    assert "error" in result.lower()


def test_save_failure_returns_error(tmp_path):
    def broken(*args):
        raise RuntimeError("private provider output")

    _, registry = setup_tools(tmp_path, broken)
    result = registry.run("core_save_research", {"title": "T", "text": "answer", "urls": []})
    assert_result(result)
    assert "error" in result.lower() and "private provider" not in result
