"""Tests for the deterministic mail safety checks."""

import pytest

from igris.mailcheck import MAX_RAW_SIZE, MailVerdict, check_message


def build_message(
    *,
    from_hdr="Alice <alice@example.com>",
    reply_to=None,
    auth_headers=(),
    body="Hello",
    html=None,
):
    lines = [
        "MIME-Version: 1.0",
        "From: " + from_hdr,
        "To: bob@example.org",
        "Subject: test",
    ]
    if reply_to is not None:
        lines.append("Reply-To: " + reply_to)
    for header in auth_headers:
        lines.append("Authentication-Results: " + header)
    if html is None:
        lines.append("Content-Type: text/plain; charset=utf-8")
        lines.append("")
        lines.append(body)
    else:
        lines.append('Content-Type: multipart/alternative; boundary="BOUND"')
        lines.append("")
        lines.append("--BOUND")
        lines.append("Content-Type: text/plain; charset=utf-8")
        lines.append("")
        lines.append(body)
        lines.append("--BOUND")
        lines.append("Content-Type: text/html; charset=utf-8")
        lines.append("")
        lines.append(html)
        lines.append("--BOUND--")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def test_pass_results_are_reported():
    raw = build_message(
        auth_headers=[
            "mx.google.com; spf=pass smtp.mailfrom=example.com; "
            "dkim=pass header.d=example.com; dmarc=pass header.from=example.com"
        ]
    )
    verdict = check_message(raw)
    assert (verdict.spf, verdict.dkim, verdict.dmarc) == ("pass", "pass", "pass")
    assert verdict.from_domain == "example.com"
    assert verdict.reply_to_domain == ""
    assert verdict.flags == ()
    assert verdict.risk == "low"


def test_fail_results_and_high_risk():
    raw = build_message(
        auth_headers=[
            "mx.google.com; spf=fail smtp.mailfrom=example.com; "
            "dkim=fail header.d=example.com; dmarc=fail header.from=example.com"
        ]
    )
    verdict = check_message(raw)
    assert (verdict.spf, verdict.dkim, verdict.dmarc) == ("fail", "fail", "fail")
    assert "auth_failed" in verdict.flags
    assert verdict.risk == "high"


def test_forged_auth_results_from_other_authserv_ignored():
    raw = build_message(
        auth_headers=[
            "evil.example; spf=pass; dkim=pass; dmarc=pass",
            "mx.google.com; spf=fail smtp.mailfrom=example.com; "
            "dkim=fail header.d=example.com; dmarc=fail header.from=example.com",
        ]
    )
    verdict = check_message(raw)
    assert (verdict.spf, verdict.dkim, verdict.dmarc) == ("fail", "fail", "fail")
    assert verdict.risk == "high"


def test_only_forged_auth_results_falls_back_to_none():
    raw = build_message(
        auth_headers=["evil.example; spf=pass; dkim=pass; dmarc=pass"]
    )
    verdict = check_message(raw)
    assert (verdict.spf, verdict.dkim, verdict.dmarc) == ("none", "none", "none")
    assert verdict.risk == "medium"


def test_topmost_matching_authserv_wins():
    raw = build_message(
        auth_headers=[
            "mx.google.com; spf=pass; dkim=pass; dmarc=pass",
            "mx.google.com; spf=fail; dkim=fail; dmarc=fail",
        ]
    )
    verdict = check_message(raw)
    assert (verdict.spf, verdict.dkim, verdict.dmarc) == ("pass", "pass", "pass")


def test_auth_failed_flag_for_softfail():
    raw = build_message(
        auth_headers=["mx.google.com; spf=softfail; dkim=none; dmarc=none"]
    )
    verdict = check_message(raw)
    assert "auth_failed" in verdict.flags
    assert verdict.risk == "medium"


def test_reply_to_mismatch_flag():
    raw = build_message(
        from_hdr="Alice <alice@example.com>",
        reply_to="Bob <bob@other.example>",
    )
    verdict = check_message(raw)
    assert verdict.from_domain == "example.com"
    assert verdict.reply_to_domain == "other.example"
    assert "reply_to_mismatch" in verdict.flags


def test_reply_to_same_domain_is_not_flagged():
    raw = build_message(
        from_hdr="Alice <alice@example.com>",
        reply_to="Alice <support@example.com>",
    )
    verdict = check_message(raw)
    assert "reply_to_mismatch" not in verdict.flags


def test_lookalike_domain_close_match():
    raw = build_message(from_hdr="Alice <alice@examp1e.com>")
    verdict = check_message(raw, known_domains=("example.com",))
    assert verdict.from_domain == "examp1e.com"
    assert "lookalike_domain" in verdict.flags
    assert verdict.risk == "high"


def test_lookalike_domain_xn_label():
    raw = build_message(from_hdr="Alice <alice@xn--80ak6aa92e.com>")
    verdict = check_message(raw)
    assert "lookalike_domain" in verdict.flags
    assert verdict.risk == "high"


def test_known_domain_is_not_lookalike():
    raw = build_message(from_hdr="Alice <alice@example.com>")
    verdict = check_message(raw, known_domains=("example.com",))
    assert "lookalike_domain" not in verdict.flags


def test_display_name_spoof_flag():
    raw = build_message(
        from_hdr='"example.com" <alice@other.example>',
        body="Hello",
    )
    verdict = check_message(raw, known_domains=("example.com",))
    assert verdict.from_domain == "other.example"
    assert "display_name_spoof" in verdict.flags
    assert verdict.risk == "high"


def test_link_mismatch_flag():
    raw = build_message(
        html='<p><a href="http://evil.example/login">example.com</a></p>'
    )
    verdict = check_message(raw)
    assert "link_mismatch" in verdict.flags
    assert verdict.risk == "high"


def test_matching_anchor_does_not_flag():
    raw = build_message(
        html='<p><a href="http://example.com/login">example.com</a></p>'
    )
    verdict = check_message(raw)
    assert "link_mismatch" not in verdict.flags
    assert verdict.links == ("http://example.com/login",)


def test_query_string_and_fragment_removed():
    raw = build_message(
        body="See http://example.com/page?token=secret#section now",
    )
    verdict = check_message(raw)
    assert verdict.links == ("http://example.com/page",)


def test_links_are_unique_across_text_and_html():
    raw = build_message(
        body="First http://example.com/a?x=1 and http://example.com/a",
        html='<a href="https://example.com/b?y=2#frag">b</a>',
    )
    verdict = check_message(raw)
    assert verdict.links == (
        "http://example.com/a",
        "https://example.com/b",
    )


def test_link_cap():
    body = " ".join(f"http://example.com/{index}" for index in range(60))
    raw = build_message(body=body)
    verdict = check_message(raw)
    assert len(verdict.links) == 50
    assert verdict.links[0] == "http://example.com/0"
    assert verdict.links[-1] == "http://example.com/49"


def test_non_http_links_are_ignored():
    raw = build_message(
        body="mailto:alice@example.com and ftp://example.com/file",
    )
    verdict = check_message(raw)
    assert verdict.links == ()


def test_malformed_mail_does_not_raise():
    broken_base64 = (
        b"From: a@example.com\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"!!! not base64 !!!"
    )
    for raw in (b"\xff\xfe\x00garbage", broken_base64, b""):
        verdict = check_message(raw)
        assert isinstance(verdict, MailVerdict)
        assert verdict.risk in ("medium", "high")


def test_size_limit():
    oversized = b"a" * (MAX_RAW_SIZE + 1)
    with pytest.raises(ValueError):
        check_message(oversized)


def test_risk_levels_low_medium_high():
    low = check_message(
        build_message(
            auth_headers=[
                "mx.google.com; spf=pass; dkim=pass; dmarc=pass"
            ]
        )
    )
    assert low.risk == "low"

    medium = check_message(build_message())
    assert medium.dmarc == "none"
    assert medium.risk == "medium"

    high = check_message(
        build_message(
            auth_headers=["mx.google.com; spf=pass; dkim=pass; dmarc=fail"]
        )
    )
    assert high.risk == "high"


def mail(auth="", sender="Sender <a@example.com>", html=""):
    headers = auth + f"From: {sender}\r\nSubject: s\r\n"
    if html:
        headers += "Content-Type: text/html; charset=utf-8\r\n"
    return (headers + "\r\n" + html + "\r\n").encode("utf-8")


REAL = "Authentication-Results: mx.google.com; dkim=fail header.s=x; spf=fail; dmarc=fail\r\n"


def test_forged_header_with_the_trusted_id_below_the_real_one_is_ignored():
    forged = "Authentication-Results: mx.google.com; dkim=pass; spf=pass; dmarc=pass\r\n"
    verdict = check_message(mail(REAL + forged))
    assert (verdict.spf, verdict.dkim, verdict.dmarc) == ("fail", "fail", "fail")
    assert verdict.risk == "high"


@pytest.mark.parametrize("planted", [
    "dkim=fail header.s=dmarc=pass; dmarc=fail",
    "dkim=fail (dmarc=pass) ; dmarc=fail",
    "spf=fail smtp.mailfrom=\"dmarc=pass\"; dmarc=fail",
    "spf=fail (a (nested dmarc=pass) comment); dmarc=fail",
])
def test_result_planted_inside_a_value_or_comment_does_not_count(planted):
    verdict = check_message(mail(f"Authentication-Results: mx.google.com; {planted}\r\n"))
    assert verdict.dmarc == "fail" and verdict.risk == "high"


def test_authserv_id_may_carry_a_version_and_mixed_case():
    verdict = check_message(mail("Authentication-Results: MX.Google.com 1; DMARC=Pass\r\n"))
    assert verdict.dmarc == "pass"


@pytest.mark.parametrize("result", ["permerror", "temperror", "bestguesspass", "neutral"])
def test_only_a_real_dmarc_pass_is_low_risk(result):
    assert check_message(mail(f"Authentication-Results: mx.google.com; dmarc={result}\r\n")).risk == "medium"


def test_user_info_is_removed_from_links_and_the_real_host_shows():
    html = '<a href="https://bank.com:secret@evil.test/login?t=1">open</a>'
    assert check_message(mail(html=html)).links == ("https://evil.test/login",)


def test_subdomain_of_a_known_domain_is_not_a_lookalike():
    verdict = check_message(mail(sender="Hub <a@a.github.com>"), known_domains=("github.com",))
    assert "lookalike_domain" not in verdict.flags


def test_display_name_needs_the_whole_domain_and_allows_its_subdomains():
    known = ("example.com",)
    assert "display_name_spoof" not in check_message(
        mail(sender='"notexample.com" <a@other.test>'), known_domains=known).flags
    assert "display_name_spoof" not in check_message(
        mail(sender='"example.com" <a@mail.example.com>'), known_domains=known).flags
    assert "display_name_spoof" in check_message(
        mail(sender='"example.com support" <a@other.test>'), known_domains=known).flags


@pytest.mark.parametrize("text,href,flagged", [
    ("example.com", "https://click.example.com/x", False),
    ("www.example.com", "https://example.com/", False),
    ("v1.2", "https://other.test/", False),
    ("example.com", "https://notexample.com/", True),
    ("bank.com", "https://bank.com@evil.test/", True),
])
def test_anchor_mismatch_rules(text, href, flagged):
    verdict = check_message(mail(html=f'<a href="{href}">{text}</a>'))
    assert ("link_mismatch" in verdict.flags) is flagged


def test_internal_failure_is_marked_and_stays_medium(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr("igris.mailcheck._check", broken)
    verdict = check_message(b"x")
    assert verdict.flags == ("unreadable",) and verdict.risk == "medium"



@pytest.mark.parametrize(
    "bad", ["fail", "softfail", "temperror", "permerror"]
)
@pytest.mark.parametrize("method", ["spf", "dmarc"])
@pytest.mark.parametrize("bad_first", [True, False])
def test_a_bad_result_beats_a_pass_in_the_same_header(method, bad, bad_first):
    entries = [f"{method}=pass", f"{method}={bad}"]
    if bad_first:
        entries.reverse()

    verdict = check_message(
        build_message(auth_headers=["mx.google.com; " + "; ".join(entries)])
    )

    assert getattr(verdict, method) == bad


def test_conflicting_dmarc_results_are_high_risk():
    verdict = check_message(
        build_message(auth_headers=["mx.google.com; dmarc=pass; dmarc=fail"])
    )

    assert verdict.dmarc == "fail"
    assert verdict.risk == "high"


def test_the_worst_of_several_bad_results_is_kept():
    verdict = check_message(
        build_message(
            auth_headers=["mx.google.com; dmarc=temperror; dmarc=fail; dmarc=softfail"]
        )
    )

    assert verdict.dmarc == "fail"


def test_a_second_dkim_signature_failing_does_not_cancel_a_pass():
    # Mail often carries several DKIM signatures, and RFC 6376 treats a failed
    # one as absent, so DKIM stays out of the worst-result rule.
    verdict = check_message(
        build_message(
            auth_headers=[
                "mx.google.com; dkim=pass header.d=example.com; "
                "dkim=fail header.d=esp.example; dmarc=pass"
            ]
        )
    )

    assert verdict.dkim == "pass"
    assert verdict.risk == "low"


def test_one_valid_dkim_signature_is_enough_in_any_order():
    verdict = check_message(
        build_message(
            auth_headers=[
                "mx.google.com; dkim=fail header.d=esp.example; "
                "dkim=pass header.d=example.com; dmarc=pass"
            ]
        )
    )

    assert verdict.dkim == "pass"
    assert verdict.risk == "low"
