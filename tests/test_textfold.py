"""Golden tests for igris.textfold.

The expected outputs were recorded from the seven private folding helpers that lived in
assistant, mail_decide, mailtriage, modelbench, routing, telegram_chat and people before they
were moved here, so every call site keeps its exact behaviour (including where the variants
differ from each other).
"""

import pytest

from igris import textfold

GOLDEN = {
    'assistant': [
        ('\u0130STANBUL', 'istanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', 'sisli'),
        ('\xe7\u011f\xf6\xfc\u015f', 'cgous'),
        ('\xc7\u011e\xd6\xdc\u015e', 'cgous'),
        ('K\u0130RA \xf6demesi', 'kira odemesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'igdir ilicasi'),
        ('i\u0307stanbul', 'i\u0307stanbul'),
        ('a\u0307b', 'a\u0307b'),
        ('Ki\u0307ra', 'ki\u0307ra'),
        ('e\u0301cole', 'e\u0301cole'),
        ('\xd1and\xfa', '\xf1and\xfa'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', '  cok   bosluklu\tmetin \n'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\'tirnak\' "cift"'),
        ('Stra\xdfe', 'stra\xdfe'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed iiii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "odev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'gozluk \ufb01 ligature'),
        ('\u0150 \u0151 \u016f', '\u0151 \u0151 \u016f'),
    ],
    'mail_decide': [
        ('\u0130STANBUL', 'istanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', '\u015fi\u015fli'),
        ('\xe7\u011f\xf6\xfc\u015f', '\xe7\u011f\xf6\xfc\u015f'),
        ('\xc7\u011e\xd6\xdc\u015e', '\xe7\u011f\xf6\xfc\u015f'),
        ('K\u0130RA \xf6demesi', 'kira \xf6demesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'i\u011fdir ilicasi'),
        ('i\u0307stanbul', 'istanbul'),
        ('a\u0307b', 'ab'),
        ('Ki\u0307ra', 'kira'),
        ('e\u0301cole', 'e\u0301cole'),
        ('\xd1and\xfa', '\xf1and\xfa'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', '\xe7ok bo\u015fluklu metin'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\u2019tirnak\u2018 "\xe7ift"'),
        ('Stra\xdfe', 'strasse'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed iiii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "\xf6dev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'g\xf6zl\xfck fi ligature'),
        ('\u0150 \u0151 \u016f', '\u0151 \u0151 \u016f'),
    ],
    'mailtriage': [
        ('\u0130STANBUL', 'istanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', '\u015fi\u015fli'),
        ('\xe7\u011f\xf6\xfc\u015f', '\xe7\u011f\xf6\xfc\u015f'),
        ('\xc7\u011e\xd6\xdc\u015e', '\xe7\u011f\xf6\xfc\u015f'),
        ('K\u0130RA \xf6demesi', 'kira \xf6demesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'i\u011fdir ilicasi'),
        ('i\u0307stanbul', 'istanbul'),
        ('a\u0307b', 'a\u0307b'),
        ('Ki\u0307ra', 'kira'),
        ('e\u0301cole', 'e\u0301cole'),
        ('\xd1and\xfa', '\xf1and\xfa'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', '  \xe7ok   bo\u015fluklu\tmetin \n'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\u2019tirnak\u2018 "\xe7ift"'),
        ('Stra\xdfe', 'strasse'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed iiii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "\xf6dev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'g\xf6zl\xfck fi ligature'),
        ('\u0150 \u0151 \u016f', '\u0151 \u0151 \u016f'),
    ],
    'modelbench': [
        ('\u0130STANBUL', 'istanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', 'sisli'),
        ('\xe7\u011f\xf6\xfc\u015f', 'cgous'),
        ('\xc7\u011e\xd6\xdc\u015e', 'cgous'),
        ('K\u0130RA \xf6demesi', 'kira odemesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'igdir ilicasi'),
        ('i\u0307stanbul', 'i\u0307stanbul'),
        ('a\u0307b', 'a\u0307b'),
        ('Ki\u0307ra', 'ki\u0307ra'),
        ('e\u0301cole', 'e\u0301cole'),
        ('\xd1and\xfa', '\xf1and\xfa'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', '  cok   bosluklu\tmetin \n'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\u2019tirnak\u2018 "cift"'),
        ('Stra\xdfe', 'strasse'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed iiii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "odev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'gozluk fi ligature'),
        ('\u0150 \u0151 \u016f', '\u0151 \u0151 \u016f'),
    ],
    'routing': [
        ('\u0130STANBUL', 'istanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', 'sisli'),
        ('\xe7\u011f\xf6\xfc\u015f', 'cgous'),
        ('\xc7\u011e\xd6\xdc\u015e', 'cgous'),
        ('K\u0130RA \xf6demesi', 'kira odemesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'igdir ilicasi'),
        ('i\u0307stanbul', 'i\u0307stanbul'),
        ('a\u0307b', 'a\u0307b'),
        ('Ki\u0307ra', 'ki\u0307ra'),
        ('e\u0301cole', 'e\u0301cole'),
        ('\xd1and\xfa', '\xf1and\xfa'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', '  cok   bosluklu\tmetin \n'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\u2019tirnak\u2018 "cift"'),
        ('Stra\xdfe', 'strasse'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed iiii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "odev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'gozluk fi ligature'),
        ('\u0150 \u0151 \u016f', '\u0151 \u0151 \u016f'),
    ],
    'telegram_chat': [
        ('\u0130STANBUL', 'i\u0307stanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', 'sisli'),
        ('\xe7\u011f\xf6\xfc\u015f', 'cgous'),
        ('\xc7\u011e\xd6\xdc\u015e', 'cgous'),
        ('K\u0130RA \xf6demesi', 'ki\u0307ra odemesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'igdir ilicasi'),
        ('i\u0307stanbul', 'i\u0307stanbul'),
        ('a\u0307b', 'a\u0307b'),
        ('Ki\u0307ra', 'ki\u0307ra'),
        ('e\u0301cole', 'e\u0301cole'),
        ('\xd1and\xfa', '\xf1and\xfa'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', '  cok   bosluklu\tmetin \n'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\u2019tirnak\u2018 "cift"'),
        ('Stra\xdfe', 'stra\xdfe'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed i\u0307iii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "odev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'gozluk \ufb01 ligature'),
        ('\u0150 \u0151 \u016f', '\u0151 \u0151 \u016f'),
    ],
    'people': [
        ('\u0130STANBUL', 'istanbul'),
        ('\u0131l\u0131k', 'ilik'),
        ('\u015ei\u015fli', 'sisli'),
        ('\xe7\u011f\xf6\xfc\u015f', 'cgous'),
        ('\xc7\u011e\xd6\xdc\u015e', 'cgous'),
        ('K\u0130RA \xf6demesi', 'kira odemesi'),
        ('SINAV Tarihi', 'sinav tarihi'),
        ('I\u011fd\u0131r \u0131l\u0131cas\u0131', 'igdir ilicasi'),
        ('i\u0307stanbul', 'istanbul'),
        ('a\u0307b', 'ab'),
        ('Ki\u0307ra', 'kira'),
        ('e\u0301cole', 'ecole'),
        ('\xd1and\xfa', 'nandu'),
        ('  \xc7ok   bo\u015fluklu\tmetin \n', 'cok bosluklu metin'),
        ('\u2019T\u0131rnak\u2018 "\xe7ift"', '\u2019tirnak\u2018 "cift"'),
        ('Stra\xdfe', 'stra\xdfe'),
        ('\u01c5emal', '\u01c6emal'),
        ('MiXeD \u0130\u0131Ii', 'mixed iiii'),
        ('', ''),
        ("\xd6dev: 15 Kas\u0131m'a kadar!", "odev: 15 kasim'a kadar!"),
        ('G\xf6zl\xfck \ufb01 ligature', 'gozluk \ufb01 ligature'),
        ('\u0150 \u0151 \u016f', 'o o u'),
    ],
}

# which textfold function now serves each former variant
FUNCTIONS = {
    "assistant": textfold.fold_lower_ascii,
    "mail_decide": textfold.fold_i_words,
    "mailtriage": textfold.fold_i,
    "modelbench": textfold.fold_ascii,
    "routing": textfold.fold_ascii,
    "telegram_chat": textfold.lower_ascii,
    "people": textfold.fold_marks,
}


@pytest.mark.parametrize("variant,text,expected", [
    (variant, text, expected) for variant, rows in GOLDEN.items() for text, expected in rows])
def test_each_former_variant_keeps_its_output(variant, text, expected):
    assert FUNCTIONS[variant](text) == expected


def test_call_sites_use_the_shared_functions():
    from igris import assistant, mail_decide, mailtriage, modelbench, people, routing

    samples = [text for text, _ in GOLDEN["assistant"]]
    for module, function in ((assistant, textfold.fold_lower_ascii),
                             (mail_decide, textfold.fold_i_words),
                             (mailtriage, textfold.fold_i), (modelbench, textfold.fold_ascii),
                             (routing, textfold.fold_ascii)):
        assert [module._fold(s) for s in samples] == [function(s) for s in samples]
    assert [people.fold(s) for s in samples] == [textfold.fold_marks(s) for s in samples]


def test_edge_inputs_keep_their_old_handling():
    assert textfold.fold_i_words(None) == ""
    with pytest.raises(TypeError):
        textfold.fold_marks(None)


def test_textfold_is_a_leaf_module():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(textfold.__file__).read_text(encoding="utf-8"))
    names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names]
    names += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [n for n in names if n == "igris" or n.startswith("igris.")]
