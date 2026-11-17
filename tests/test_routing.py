from igris.routing import Decision, decide, detect_lang, classify_domain, order_free_backends


def test_domain_classifier_uses_attachment_and_keywords():
    assert classify_domain("Merhaba, nasılsın?") == "turkish_chat"
    assert classify_domain("Summarize this report") == "summarization"
    assert classify_domain("Fix this Python function") == "code"
    assert classify_domain("Call the weather tool", has_attachments=True) == "tool_use"
    assert classify_domain("Find the needle in this long document", has_attachments=True) == "long_context"
    assert classify_domain("Solve this equation") == "reasoning"


def test_free_order_respects_rank_and_config_fallback():
    configured = ["first", "second", "third"]
    assert order_free_backends(configured, [("second", 0.9), ("first", 0.8)]) == [
        "second", "first", "third"]
    assert order_free_backends(configured, []) == configured


def test_free_order_privacy_and_health_win_over_rank():
    assert order_free_backends(["training", "safe", "down"],
                               ["training", "down", "safe"],
                               training={"training"}, private=True,
                               unavailable={"down"}) == ["safe"]


def test_small_question_stays_with_igris():
    d = decide("2+2 kaç eder?")
    assert d.target == "self" and d.source == "rules"


def test_explanation_stays_with_igris():
    assert decide("Redis nedir, açıkla").target == "self"


def test_large_review_goes_to_multiagent():
    d = decide("Bu projenin tüm yapısını incele ve kapsamlı bir mimari raporu çıkar")
    assert d.target == "multiagent" and d.source == "rules"
    assert "multiagent" in d.reason


def test_turkish_uppercase():
    assert decide("TÜM PROJEYİ İNCELE VE PLANLA").target == "multiagent"


def test_override_self_beats_keywords():
    for text in ("bunu sen yanıtla: tüm projeyi incele",
                 "answer this yourself: review the whole project"):
        d = decide(text)
        assert d.target == "self" and d.source == "override"


def test_override_multiagent():
    for text in ("multiagent'a ver: 2+2", "give this to multiagent: what is 2+2"):
        d = decide(text)
        assert d.target == "multiagent" and d.source == "override"
        assert "multiagent" in d.reason


def test_gray_zone_uses_classifier_once():
    calls = []

    def fake(text):
        calls.append(text)
        return "multiagent"

    d = decide("refactor this function", classify=fake)
    assert (d.target, d.source) == ("multiagent", "classifier")
    assert len(calls) == 1
    assert decide("refactor this function", classify=lambda t: "banana").target == "self"


def test_classifier_not_called_when_rules_decide():
    calls = []

    def fake(text):
        calls.append(text)
        return "multiagent"

    decide("2+2 kaç eder?", classify=fake)
    decide("Review the whole project and plan the architecture", classify=fake)
    assert calls == []


def test_gray_zone_without_classifier_is_self():
    d = decide("refactor this function")
    assert d.target == "self"


def test_detect_lang():
    assert detect_lang("Bu nedir?") == "tr"
    assert detect_lang("What is this?") == "en"
    for ch in "ışğüöçİ":
        assert detect_lang(f"abc {ch} def") == "tr"
    assert decide("What is 2+2?").lang == "en"
    assert decide("2+2 kaç eder?").lang == "tr"
    assert isinstance(decide("hi"), Decision)


def _counting():
    calls = []

    def fake(text):
        calls.append(text)
        return "self"

    return calls, fake


def test_tum_proje_keyword_alone_is_gray_zone():
    calls, fake = _counting()
    decide("tüm projeyi gözden geçir", classify=fake)
    assert len(calls) == 1  # +2 from "tüm proje" only


def test_acikla_penalty_applies():
    calls, fake = _counting()
    d = decide("tüm projeyi açıkla", classify=fake)
    assert d.target == "self" and d.source == "rules" and calls == []


def test_module_and_file_signal():
    d = decide("refactor a.py ve modül")
    assert d.target == "multiagent" and d.source == "rules"


def test_english_uppercase_keywords():
    assert decide("REVIEW THE AUDIT").target == "multiagent"


def test_detect_lang_word_list_without_special_chars():
    assert detect_lang("bunu bana soyle") == "tr"
    assert detect_lang("tum proje") == "tr"


import pytest


@pytest.mark.parametrize("answer", ["multiagent", "Multiagent.", " MULTIAGENT\n",
                                    "'multiagent'", "multiagent - large task",
                                    "**multiagent**", "multiagent: big refactor"])
def test_classifier_answer_is_normalized(answer):
    d = decide("please review this", classify=lambda text: answer)
    assert d.target == "multiagent" and d.source == "classifier"


@pytest.mark.parametrize("answer", ["self", "Self.", "", "   ", "not multiagent",
                                    "multiagentic", None])
def test_classifier_other_answers_mean_self(answer):
    d = decide("please review this", classify=lambda text: answer)
    assert d.target == "self" and d.source == "classifier"
