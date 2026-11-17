"""Tests for igris.mailtriage.

No network access and no DNS lookups are performed anywhere in this
module; classification is pure header arithmetic and needs neither the
filesystem nor a clock.
"""

import pytest

from igris.mailtriage import LABELS, TriageRules, classify, load_rules


# ---------------------------------------------------------------- labels


def test_vip_sender_is_important():
    rules = TriageRules(vip_senders=("boss@example.com",))
    headers = {"From": "boss@example.com", "Subject": "hello"}
    assert classify(headers, rules) == "important"


def test_vip_domain_is_important():
    rules = TriageRules(vip_domains=("example.com",))
    headers = {"From": "anyone@example.com", "Subject": "hello"}
    assert classify(headers, rules) == "important"


def test_junk_word_is_junk():
    rules = TriageRules(junk_words=("viagra",))
    headers = {"From": "spam@example.org", "Subject": "cheap VIAGRA now"}
    assert classify(headers, rules) == "junk"


def test_newsletter_via_list_unsubscribe():
    rules = TriageRules()
    headers = {
        "From": "news@example.org",
        "Subject": "weekly digest",
        "List-Unsubscribe": "<https://example.org/unsub>",
    }
    assert classify(headers, rules) == "newsletter"


def test_newsletter_via_precedence_bulk():
    rules = TriageRules()
    headers = {"From": "news@example.org", "Subject": "digest", "Precedence": "Bulk"}
    assert classify(headers, rules) == "newsletter"


def test_newsletter_via_precedence_list():
    rules = TriageRules()
    headers = {"From": "news@example.org", "Subject": "digest", "Precedence": "list"}
    assert classify(headers, rules) == "newsletter"


def test_important_word_is_important():
    rules = TriageRules(important_words=("urgent",))
    headers = {"From": "someone@example.org", "Subject": "URGENT: please review"}
    assert classify(headers, rules) == "important"


def test_plain_message_is_routine():
    rules = TriageRules(important_words=("urgent",))
    headers = {"From": "someone@example.org", "Subject": "lunch tomorrow?"}
    assert classify(headers, rules) == "routine"


def test_all_labels_are_reachable():
    # Sanity check: every advertised label can actually be produced.
    produced = set()
    produced.add(classify({"From": "a@b.com"}, TriageRules(vip_senders=("a@b.com",))))
    produced.add(classify({"Subject": "junk"}, TriageRules(junk_words=("junk",))))
    produced.add(classify({"List-Unsubscribe": "<x>"}, TriageRules()))
    produced.add(classify({"Subject": "big"}, TriageRules(important_words=("big",))))
    produced.add(classify({}, TriageRules()))
    assert produced == set(LABELS)


# ----------------------------------------------------------- rule order


def test_vip_beats_junk_word():
    rules = TriageRules(
        vip_senders=("boss@example.com",),
        junk_words=("viagra",),
    )
    headers = {"From": "boss@example.com", "Subject": "viagra viagra"}
    assert classify(headers, rules) == "important"


def test_junk_word_beats_newsletter():
    rules = TriageRules(junk_words=("viagra",))
    headers = {
        "From": "news@example.org",
        "Subject": "viagra",
        "List-Unsubscribe": "<https://example.org/unsub>",
    }
    assert classify(headers, rules) == "junk"


def test_newsletter_beats_important_word():
    rules = TriageRules(important_words=("urgent",))
    headers = {
        "From": "news@example.org",
        "Subject": "urgent urgent urgent",
        "List-Unsubscribe": "<https://example.org/unsub>",
    }
    assert classify(headers, rules) == "newsletter"


def test_important_word_beats_routine():
    rules = TriageRules(important_words=("urgent",))
    headers = {"From": "x@example.org", "Subject": "urgent"}
    assert classify(headers, rules) == "important"


# ------------------------------------------------------- sender handling


def test_display_name_alone_is_not_vip():
    # The VIP address appears only in the display name; parseaddr must
    # extract the real address instead, so this is not important.
    rules = TriageRules(vip_senders=("ceo@example.com",))
    headers = {"From": "ceo@example.com (Friendly Person) <friend@example.org>"}
    assert classify(headers, rules) == "routine"


def test_quoted_display_name_alone_is_not_vip():
    # Same idea, quoted form: the real address wins, not the display name.
    rules = TriageRules(vip_senders=("ceo@example.com",))
    headers = {"From": '"ceo@example.com" <other@example.org>'}
    assert classify(headers, rules) == "routine"


def test_display_name_with_real_vip_address_is_important():
    # The actual address is the VIP one, regardless of the display name
    # or its casing.
    rules = TriageRules(vip_senders=("ceo@example.com",))
    headers = {"From": '"Chief Executive" <CEO@example.com>'}
    assert classify(headers, rules) == "important"


def test_lookalike_domain_is_not_vip():
    # evil-example.com must never match a VIP domain of example.com.
    rules = TriageRules(vip_domains=("example.com",))
    headers = {"From": "attacker@evil-example.com", "Subject": "hi"}
    assert classify(headers, rules) == "routine"


def test_missing_from_is_not_vip():
    rules = TriageRules(vip_senders=("boss@example.com",))
    headers = {"Subject": "hi"}
    assert classify(headers, rules) == "routine"


# -------------------------------------------------------- header casing


def test_header_names_and_values_are_case_insensitive():
    rules = TriageRules(
        vip_senders=("boss@example.com",),
        junk_words=("viagra",),
    )
    assert classify(
        {"FROM": "BOSS@EXAMPLE.COM", "SUBJECT": "Hello"}, rules
    ) == "important"
    assert classify({"sUbJeCt": "Cheap ViAgRa"}, rules) == "junk"
    assert classify(
        {"from": "x@y.com", "LIST-UNSUBSCRIBE": "<https://x.example>"}, rules
    ) == "newsletter"
    assert classify({"fRoM": "x@y.com", "precedence": "BULK"}, rules) == "newsletter"


# ------------------------------------------------------------ load_rules


def test_load_rules_valid():
    rules = load_rules(
        {
            "vip_senders": ["boss@example.com"],
            "vip_domains": ["example.com"],
            "important_words": ["urgent"],
            "junk_words": ["viagra"],
        }
    )
    assert rules == TriageRules(
        vip_senders=("boss@example.com",),
        vip_domains=("example.com",),
        important_words=("urgent",),
        junk_words=("viagra",),
    )


def test_load_rules_empty_mapping():
    assert load_rules({}) == TriageRules()


def test_load_rules_partial_fields():
    rules = load_rules({"junk_words": ["spam"]})
    assert rules == TriageRules(junk_words=("spam",))


def test_load_rules_rejects_unknown_field():
    with pytest.raises(ValueError):
        load_rules({"vip_senders": ["a@b.com"], "surprise": [1, 2]})


def test_load_rules_rejects_non_list_field():
    with pytest.raises(ValueError):
        load_rules({"vip_senders": "boss@example.com"})


def test_load_rules_rejects_non_string_items():
    with pytest.raises(ValueError):
        load_rules({"important_words": ["urgent", 42]})


def test_load_rules_rejects_empty_string_item():
    with pytest.raises(ValueError):
        load_rules({"junk_words": ["viagra", ""]})


def test_load_rules_rejects_none_item():
    with pytest.raises(ValueError):
        load_rules({"vip_domains": [None]})


def subject(text):
    return {"From": "someone@example.com", "Subject": text}


def test_word_must_start_at_a_word_boundary():
    rules = TriageRules(important_words=("urgent",), junk_words=("win",))
    assert classify(subject("nonurgent update"), rules) == "routine"
    assert classify(subject("Darwin notes"), rules) == "routine"
    assert classify(subject("URGENT: call me"), rules) == "important"


def test_turkish_suffixes_and_capital_dotted_i_match():
    rules = TriageRules(important_words=("fatura", "acil"))
    assert classify(subject("Faturanız hazır"), rules) == "important"
    assert classify(subject("ACİL dönüş"), rules) == "important"


def test_blank_words_in_directly_built_rules_match_nothing():
    rules = TriageRules(important_words=("",), junk_words=("", "  "))
    assert classify(subject("hello"), rules) == "routine"


@pytest.mark.parametrize(
    "word, text",
    [
        # capital subject, lower-case word (all four i letters)
        ("sınav", "SINAV sonuçları"),
        ("kira", "KİRA ödemesi"),
        ("ödev teslimi", "ÖDEV TESLİMİ yarın"),
        ("ışık", "IŞIK faturası"),
        # lower-case subject, capital word
        ("SINAV", "sınav tarihi"),
        ("KİRA", "kira hatırlatma"),
        ("ÖDEV TESLİMİ", "ödev teslimi hakkında"),
        # mixed: dotted and dotless spellings meet each other
        ("sinav", "Sınav günü"),
        ("kıra", "Kira günü"),
        ("İLAN", "ilan yayında"),
        ("ilan", "ILAN yayında"),
    ],
)
def test_turkish_i_variants_match_both_ways(word, text):
    rules = TriageRules(important_words=(word,))
    assert classify(subject(text), rules) == "important"


def test_turkish_capital_junk_word_matches():
    rules = TriageRules(junk_words=("kazandınız",))
    assert classify(subject("TEBRİKLER KAZANDINIZ"), rules) == "junk"


def test_turkish_i_fold_keeps_word_start_anchor():
    rules = TriageRules(important_words=("sınav",))
    assert classify(subject("ARASINAV notu"), rules) == "routine"


def test_word_of_only_a_combining_dot_matches_nothing():
    rules = load_rules({"junk_words": ["̇"]})
    assert classify({"Subject": ""}, rules) == "routine"
    assert classify(subject("hello"), rules) == "routine"


def test_combining_dot_not_after_an_i_keeps_word_boundary():
    rules = TriageRules(important_words=("urgent",))
    assert classify(subject("ẋurgent"), rules) == "important"


def test_combining_dot_after_i_is_folded_away():
    rules = TriageRules(important_words=("kira",))
    assert classify(subject("Ki̇ra"), rules) == "important"
