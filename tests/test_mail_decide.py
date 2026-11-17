from datetime import date
import json

import pytest

from igris import mail_decide
from igris.mail_decide import decide

TODAY = date(2026, 11, 9)
RENT_BODY = ("Merhaba,\n"
             "Kasım ayı kirası 500 EUR, lütfen 15 Kasım 2026 tarihine kadar hesabıma yatır.\n"
             "Teşekkürler, Ali")


def mail(**kw):
    base = {"sender": "Ali <ali@example.lt>", "subject": "Kasım kirası", "body": RENT_BODY,
            "labels": ("INBOX",), "unsubscribe": False, "triage": "routine", "risk": "low",
            "flags": (), "dmarc": "pass"}
    base.update(kw)
    return base


class Model:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, system, prompt):
        self.calls.append((system, prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


RENT_ANSWER = {
    "category": "gorev", "reason": "Ev sahibi Kasım kirasını 15 Kasım'a kadar istiyor.",
    "task": {"title": "Kasım kirasını öde (500 EUR)", "kind": "payment", "due": "2026-11-15",
             "start": None, "branch": "kisisel", "mandatory": True, "confidence": 0.95},
    "evidence": "Kasım ayı kirası 500 EUR, lütfen 15 Kasım 2026 tarihine kadar hesabıma yatır.",
}


def test_rent_mail_is_a_task_that_notifies():
    model = Model(RENT_ANSWER)
    d = decide(mail(), model, today=TODAY)
    assert d.category == "gorev" and d.notify and d.stage == "model"
    assert d.task["kind"] == "payment" and d.task["due"] == "2026-11-15"
    assert d.task["mandatory"] and d.task["confidence"] == 0.95
    system, prompt = model.calls[0]
    assert "strict" not in system or True
    assert "Today: 2026-11-09" in prompt and "Kasım kirası" in prompt and "500 EUR" in prompt


def test_task_without_its_evidence_in_the_mail_is_dropped_and_doubted():
    answer = dict(RENT_ANSWER, evidence="Kira 1 Aralık'ta ödenecek.")
    d = decide(mail(), Model(answer), today=TODAY)
    assert d.category == "gorev" and d.task is None  # never a dated task it cannot prove
    assert d.doubt == "kira" and d.notify  # but never silent either


@pytest.mark.parametrize("change", [{"mandatory": False}, {"confidence": 0.6}])
def test_optional_or_unsure_task_is_kept_but_quiet(change):
    answer = dict(RENT_ANSWER, task=dict(RENT_ANSWER["task"], **change))
    d = decide(mail(), Model(answer), today=TODAY)
    assert d.category == "gorev" and d.task is not None and not d.notify


def test_bad_model_answers_fall_back_to_the_rules_and_say_the_model_failed():
    for answer in ("not json", {"category": "spam"}, RuntimeError("limit"), "[1, 2]"):
        d = decide(mail(), Model(answer), today=TODAY)
        assert d.stage == "model_failed" and d.category == "gorev" and d.notify
        assert d.task is None and "model okuyamadı, kendin kontrol et" in d.reason


def test_rent_subject_notifies_when_the_model_raises():
    d = decide(mail(subject="Kira ödemesi", body="Detaylar ekte."), Model(RuntimeError("quota")),
               today=TODAY)
    assert d.category == "gorev" and d.notify and d.stage == "model_failed"
    assert d.reason.startswith("Görev olabilir (")


def test_no_model_is_told_apart_from_a_failed_model():
    assert decide(mail(), None).stage == "fallback"
    assert decide(mail(), Model(RuntimeError("down"))).stage == "model_failed"
    # a mail without a task word stays quiet either way
    quiet = decide(mail(subject="Merhaba", body="Nasılsın?"), Model(RuntimeError("down")))
    assert quiet.category == "bilgi" and not quiet.notify and quiet.stage == "model_failed"


def test_model_json_inside_a_code_fence_is_read():
    fenced = "```json\n" + json.dumps(RENT_ANSWER) + "\n```"
    assert decide(mail(), Model(fenced), today=TODAY).notify


def test_task_fields_are_normalised():
    task = dict(RENT_ANSWER["task"], kind="party", branch="mars", confidence=3,
                due="2026-11-15T18:00", start="yarın")
    d = decide(mail(), Model(dict(RENT_ANSWER, task=task)), today=TODAY)
    assert d.task["kind"] == "other" and d.task["branch"] == "kisisel"
    assert d.task["confidence"] == 1.0 and d.task["start"] is None
    assert d.task["due"].startswith("2026-11-15T18:00") and len(d.task["due"]) > 16  # offset added


def test_high_risk_is_security_without_asking_a_model():
    model = Model(RENT_ANSWER)
    d = decide(mail(risk="high", flags=("display_name_spoof",), dmarc="fail"), model)
    assert d.category == "guvenlik" and d.notify and d.stage == "security"
    assert "DMARC" in d.reason and "taklit" in d.reason and model.calls == []


@pytest.mark.parametrize("subject", ["Security alert: new sign-in on Windows",
                                     "Şifre sıfırlama isteği", "Google hesabınızda şüpheli etkinlik"])
def test_account_security_alerts_notify(subject):
    d = decide(mail(subject=subject, body="..."), Model(RENT_ANSWER))
    assert d.category == "guvenlik" and d.notify


def test_unverified_security_alert_says_so():
    d = decide(mail(subject="Security alert", dmarc="fail", body=""), None)
    assert d.category == "guvenlik" and "doğrulanamadı" in d.reason


@pytest.mark.parametrize("facts, category", [
    ({"labels": ("INBOX", "CATEGORY_PROMOTIONS"), "subject": "Yeni sezon", "body": ""}, "tanitim"),
    ({"unsubscribe": True, "subject": "%50 indirim bu hafta", "body": ""}, "tanitim"),
    ({"triage": "junk", "subject": "x", "body": ""}, "tanitim"),
    ({"labels": ("CATEGORY_SOCIAL",), "subject": "Ali seni etiketledi", "body": ""}, "rutin"),
    ({"sender": "GitHub <noreply@github.com>", "subject": "Your build passed", "body": "ok"}, "rutin"),
])
def test_cheap_rules_decide_without_a_model(facts, category):
    model = Model(RENT_ANSWER)
    d = decide(mail(**facts), model)
    assert d.category == category and not d.notify and model.calls == []


def test_a_task_word_keeps_a_promotion_or_notifier_mail_for_the_reader():
    model = Model(RENT_ANSWER)
    decide(mail(sender="Moodle <noreply@example.edu>", subject="Assignment due tomorrow", body=""), model)
    decide(mail(labels=("CATEGORY_PROMOTIONS",), subject="Ödeme hatırlatması"), model)
    assert len(model.calls) == 2


def test_without_a_model_a_task_word_notifies_and_nothing_else_does():
    assert decide(mail(), None).category == "gorev"
    assert decide(mail(), None).notify
    quiet = decide(mail(subject="Merhaba", body="Nasılsın?"), None)
    assert quiet.category == "bilgi" and not quiet.notify


def test_prompt_is_bounded_and_clean():
    prompt = mail_decide.prompt_for(mail(sender="a\x00b", body="x" * 10000), TODAY)
    assert "\x00" not in prompt and len(prompt) < mail_decide.BODY_LIMIT + 400


def test_model_security_verdict_notifies():
    answer = {"category": "guvenlik", "reason": "Banka taklidi, şifre istiyor.", "task": None,
              "evidence": ""}
    d = decide(mail(subject="Hesabınızı doğrulayın", body="Şifrenizi girin"), Model(answer))
    assert d.category == "guvenlik" and d.notify and d.task is None


def test_a_task_word_inside_a_link_or_address_is_no_task():
    d = decide(mail(subject="Yeni ürünler", body="https://shop.example/yeni info@exam.example"), None)
    assert d.category == "bilgi" and not d.notify


# ---------- a working model that files an obligation as information is doubted ----------

def answer(category, reason="Bilgilendirme."):
    return {"category": category, "reason": reason, "task": None, "evidence": ""}


def test_a_model_that_calls_a_rent_subject_information_is_doubted_and_notifies():
    d = decide(mail(subject="Kira ödemesi", body="Detaylar ekte."), Model(answer("bilgi")),
               today=TODAY)
    assert d.category == "bilgi" and d.stage == "model" and d.notify
    assert d.doubt == "ödeme" and "'ödeme'" in d.reason and "Bilgilendirme." in d.reason
    assert d.reason.startswith("Model görev değil dedi")


def test_a_doubted_routine_answer_notifies_too():
    d = decide(mail(subject="Fatura hatırlatması", body="..."), Model(answer("rutin")))
    assert d.category == "rutin" and d.doubt == "fatura" and d.notify


@pytest.mark.parametrize("facts", [
    {"unsubscribe": True},
    {"labels": ("INBOX", "CATEGORY_PROMOTIONS")},
    {"labels": ("INBOX", "CATEGORY_SOCIAL")},
])
def test_a_doubted_bulk_mail_is_marked_but_quiet(facts):
    model = Model(answer("bilgi"))
    d = decide(mail(subject="Kira ödemesi", body="Detaylar ekte.", **facts), model)
    assert len(model.calls) == 1  # the subject task word sends it to the model
    assert d.category == "bilgi" and d.doubt == "ödeme" and not d.notify
    assert "telefona bildirilmedi" in d.reason


def test_a_doubted_promotion_is_quiet():
    d = decide(mail(subject="Kira ödemesi", body="Detaylar ekte."), Model(answer("tanitim")))
    assert d.category == "tanitim" and d.doubt == "ödeme" and not d.notify
    assert "tanıtım" in d.reason


def test_a_task_word_only_in_the_body_creates_no_doubt():
    d = decide(mail(subject="Merhaba", body="Kira konusunu sonra konuşuruz."),
               Model(answer("bilgi")))
    assert d.doubt is None and not d.notify and d.reason == "Bilgilendirme."


def test_a_task_word_inside_a_link_in_the_subject_creates_no_doubt():
    d = decide(mail(subject="Yeni ürünler www.exam.example", body=""), Model(answer("bilgi")))
    assert d.doubt is None and not d.notify


def test_task_and_security_answers_are_never_doubted():
    assert decide(mail(), Model(RENT_ANSWER), today=TODAY).doubt is None
    unsure = dict(RENT_ANSWER, task=dict(RENT_ANSWER["task"], confidence=0.5))
    d = decide(mail(subject="Kira ödemesi"), Model(unsure), today=TODAY)
    assert d.category == "gorev" and d.doubt is None and not d.notify and d.task


def test_a_task_answer_without_a_task_is_doubted():
    d = decide(mail(subject="Kira ödemesi"), Model(dict(RENT_ANSWER, task=None)), today=TODAY)
    assert d.category == "gorev" and d.task is None and d.doubt == "ödeme" and d.notify
    security = {"category": "guvenlik", "reason": "Taklit.", "task": None, "evidence": ""}
    d = decide(mail(subject="Kira ödemesi"), Model(security))
    assert d.category == "guvenlik" and d.doubt is None and d.notify


def test_rules_and_fallbacks_are_never_doubted():
    assert decide(mail(), None).doubt is None
    assert decide(mail(), Model(RuntimeError("down"))).doubt is None


# ---------- promotions and social tab mail: the start of the body counts ----------

@pytest.mark.parametrize("facts, category", [
    ({"labels": ("INBOX", "CATEGORY_PROMOTIONS"), "subject": "Yeni sezon"}, "tanitim"),
    ({"unsubscribe": True, "subject": "%50 indirim bu hafta"}, "tanitim"),
    ({"labels": ("CATEGORY_SOCIAL",), "subject": "Ali seni etiketledi"}, "rutin"),
])
def test_a_bulk_rule_hands_a_body_task_word_to_the_model(facts, category):
    model = Model(answer("bilgi", "Dönem kaydı hatırlatması."))
    d = decide(mail(body="Bahar dönemi kayıt son günü 20 Kasım.", **facts), model)
    assert len(model.calls) == 1 and d.stage == "model" and d.category == "bilgi"
    assert d.doubt is None and not d.notify  # the subject has no task word
    model = Model(answer("bilgi"))
    d = decide(mail(body="Yeni ürünlere göz at.", **facts), model)
    assert model.calls == [] and d.category == category and d.stage == "rule"


def test_a_body_task_word_after_600_characters_or_in_a_link_keeps_the_rule():
    model = Model(answer("bilgi"))
    late = "x " * 400 + "kira"
    linked = "Bak: https://shop.example/fatura ve info@exam.example"
    for body in (late, linked):
        d = decide(mail(labels=("CATEGORY_PROMOTIONS",), subject="Yeni sezon", body=body), model)
        assert d.category == "tanitim" and d.stage == "rule"
    assert model.calls == []


def test_a_promotion_with_a_body_task_word_and_a_failing_model_stays_quiet():
    d = decide(mail(labels=("INBOX", "CATEGORY_PROMOTIONS"), subject="Yeni sezon",
                    body="Fatura adresini güncelle, yeni ürünler burada."),
               Model(RuntimeError("quota")))
    assert d.stage == "model_failed" and not d.notify and d.category == "tanitim"
    assert d.reason == "Toplu gönderimde görev kelimesi geçiyor (fatura); model okuyamadı"


@pytest.mark.parametrize("facts, category", [
    ({"unsubscribe": True}, "tanitim"),
    ({"labels": ("CATEGORY_SOCIAL",)}, "rutin"),
])
def test_bulk_mail_with_a_body_task_word_stays_quiet_without_a_model(facts, category):
    d = decide(mail(subject="Haftanın özeti", body="Bu hafta sınav haftası.", **facts), None)
    assert not d.notify and d.category == category and d.stage == "fallback"
    assert "Toplu gönderimde" in d.reason


def test_personal_mail_with_a_body_task_word_still_notifies_when_the_model_fails():
    d = decide(mail(subject="Merhaba", body="Yarınki sınav 10:00'da."), Model(RuntimeError("x")))
    assert d.category == "gorev" and d.notify and d.stage == "model_failed"
    assert d.reason.startswith("Görev olabilir (sınav)")


@pytest.mark.parametrize("text, word", [
    # the link or address goes, the word beside it stays
    ("Exam(teacher@university.example)", "exam"),
    ("Sınav(ogretmen@okul.example)", "sınav"),
    ("Kira:ödeme", "ödeme"),
    ("kira, https://a.example/x.", "kira"),
    ("Final.Exam", "exam"),
    ("Ödev.pdf ektedir", "ödev"),
    ("Sınav sonuçları: sinav.edu.tr", "sınav"),
    # a word only inside a link or an address is no task
    ("https://x.example/odeme-fatura", None),
    ("portal.example/exam", None),
    ("Bak:portal.example/kira", None),
    ("ftp://school.example/exam", None),
    ("www.exam.example", None),
    ("info@exam.example", None),
    ("shop.example", None),
    ("Bak: shop.example ve portal.example/kira", None),
    # a bare label.tld whose label is a task word is a file name as much as a domain
    ("exam.com", "exam"),
    ("final.ai dosyasini yarina hazirla.", "final"),
    ("sinav.pdf", "sınav"),
    ("ödev.docx", "ödev"),  # "odev" without ö is no task word at all, link or not
    ("odev.docx", None),
    ("FATURA.PDF ekte", "fatura"),
])
def test_task_words_ignore_only_the_link_or_address_itself(text, word):
    assert mail_decide._task_word(text) == word


def test_a_promotion_naming_a_task_file_in_its_body_goes_to_the_model():
    promo = mail(labels=("CATEGORY_PROMOTIONS",), subject="Yeni sezon",
                 body="final.ai dosyasini yarina hazirla.")
    assert mail_decide.rule_decision(promo) is None


@pytest.mark.parametrize("evidence", ["̇", " ̇ ", "  ", "ab", "̇ȧ"])
def test_evidence_that_folds_to_almost_nothing_proves_no_task_but_is_not_silent(evidence):
    d = decide(mail(), Model(dict(RENT_ANSWER, evidence=evidence)), today=TODAY)
    assert d.category == "gorev" and d.task is None
    assert d.doubt and d.notify and "dayandığı cümle mailde bulunamadı" in d.reason


@pytest.mark.parametrize("subject, body, evidence, word", [
    ("通知", "付款", "付款", "görev"),  # two CJK characters: real, but too short for a task
    ("Kira", "Kira: OK", "OK", "kira"),  # two Latin letters
    ("Merhaba", RENT_BODY, "Kira 1 Aralık'ta ödenecek.", "görev"),  # not in the mail
    ("Merhaba", RENT_BODY, "̇", "görev"),  # folds to nothing
])
def test_a_task_answer_whose_task_is_rejected_is_doubted_and_notifies(subject, body, evidence,
                                                                       word):
    answer = dict(RENT_ANSWER, evidence=evidence,
                  task=dict(RENT_ANSWER["task"], confidence=1.0))
    d = decide(mail(subject=subject, body=body), Model(answer), today=TODAY)
    assert d.category == "gorev" and d.task is None and d.stage == "model"
    assert d.doubt == word and d.notify


def test_a_task_answer_whose_task_is_rejected_in_bulk_mail_is_doubted_but_quiet():
    answer = dict(RENT_ANSWER, evidence="OK")
    d = decide(mail(subject="Haftalık duyuru", body="OK", unsubscribe=True), Model(answer),
               today=TODAY)
    assert d.task is None and d.doubt == "görev" and not d.notify
    assert "telefona bildirilmedi" in d.reason


def test_short_real_evidence_still_counts():
    d = decide(mail(body="Kira 500 EUR."), Model(dict(RENT_ANSWER, evidence="500")), today=TODAY)
    assert d.task is not None


CASED_SUBJECTS = [
    "SINAV TARİHİ", "Sınav tarihi", "sınav tarihi", "sinav tarihi", "SINAV",
    "KİRA ÖDEMESİ", "Kira", "kira", "KIRA", "ÖDEME HATIRLATMASI", "FATURANIZ HAZIR",
    "Ödev TESLİMİ yarın", "TESLIM", "Bahar dönemi KAYIT işlemleri", "MIDTERM EXAM",
    "Önemli: Final Sınavı Programı", "[DUYURU] SON TESLİM 20 KASIM",
]


@pytest.mark.parametrize("subject", CASED_SUBJECTS)
def test_task_words_match_in_every_casing(subject):
    assert mail_decide._task_word(subject)
    # rules: a tab rule does not file it without a reader
    tab = mail(subject=subject, body="", labels=("CATEGORY_PROMOTIONS",))
    assert mail_decide.rule_decision(tab) is None
    # fallback: a subject task word alerts from any tab
    assert mail_decide.fallback_decision(tab).notify
    # doubt: a model that calls it information is doubted
    d = decide(mail(subject=subject, body=""), Model(answer("bilgi")))
    assert d.doubt and d.notify


@pytest.mark.parametrize("a, b", [
    ("SINAV", "sınav"), ("SINAV", "sinav"), ("KİRA", "kira"), ("KİRA", "Kira"), ("KIRA", "kira"),
    ("ÖDEME", "ödeme"), ("FATURA", "fatura"), ("TESLİM", "teslim"), ("TESLIM", "teslim"),
    ("ki̇ra", "kira"), ("İŞLEM GEREKLİ", "işlem gerekli"),
])
def test_the_fold_makes_turkish_i_variants_equal(a, b):
    assert mail_decide._fold(a) == mail_decide._fold(b)


@pytest.mark.parametrize("subject", ["Merhaba", "Yeni sezon", "HAFTALIK BÜLTEN", "Sinema gecesi"])
def test_the_fold_adds_no_match_to_plain_subjects(subject):
    assert mail_decide._task_word(subject) is None


def test_capital_security_words_and_evidence_match_too():
    assert decide(mail(subject="ŞÜPHELİ GİRİŞ DENEMESİ", body=""), None).category == "guvenlik"
    line = "KASIM KİRASI 500 EUR, 15 KASIM 2026 TARİHİNE KADAR ÖDENMELİ."
    task_answer = dict(RENT_ANSWER, evidence="Kasım kirası 500 EUR, 15 Kasım 2026 tarihine "
                                             "kadar ödenmeli.")
    d = decide(mail(subject="KİRA", body=line), Model(task_answer), today=TODAY)
    assert d.task is not None and d.notify


@pytest.mark.parametrize("subject", ["Kira ödemesi", "Exam(teacher@university.example)",
                                     "Fatura: https://x.example/a", "Merhaba", "Final.Exam"])
def test_a_subject_that_can_be_doubted_always_alerts_in_the_fallback(subject):
    # so a mail first filed quietly by the fallback can never be doubted on a re-read
    if mail_decide._task_word(subject):
        assert decide(mail(subject=subject, body="", unsubscribe=True), None).notify


@pytest.mark.parametrize("facts", [
    {}, {"unsubscribe": True}, {"labels": ("CATEGORY_PROMOTIONS",)},
    {"labels": ("CATEGORY_SOCIAL",)},
])
def test_a_subject_task_word_notifies_from_any_tab_when_the_model_fails(facts):
    d = decide(mail(subject="Fatura son ödeme günü", body="...", **facts), Model(RuntimeError("x")))
    assert d.category == "gorev" and d.notify


def test_system_prompt_uses_a_generic_owner_sentence_by_default():
    from igris import mail_decide

    assert mail_decide.DEFAULT_OWNER_CONTEXT in mail_decide.system_prompt()
    assert mail_decide.SYSTEM == mail_decide.system_prompt("")
    assert "Erasmus" not in mail_decide.SYSTEM and "university student" not in mail_decide.SYSTEM


def test_owner_context_reaches_the_model_prompt():
    model = Model(RENT_ANSWER)
    decide(mail(), model, today=TODAY, owner_context="The owner  is a\nnurse.")
    system, _ = model.calls[0]
    assert "The owner is a nurse." in system
    assert "busy individual" not in system
