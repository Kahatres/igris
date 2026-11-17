"""Offline login asset contracts; lifecycle and geometry use Node's fake browser."""

from html.parser import HTMLParser
from pathlib import Path
import re

from igris.web import _STATIC_RE, _STATIC_TYPES


STATIC = Path(__file__).resolve().parent.parent / "igris" / "static"


class LoginMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_login_intro_is_a_local_classic_script_before_the_card():
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    tags = LoginMarkup(html).tags
    scripts = [attrs for tag, attrs in tags if tag == "script"]
    assert {"src": "/static/intro.js"} in scripts
    assert {"src": "/static/intro_art.js"} in scripts
    assert html.index('/static/intro_art.js') < html.index('/static/intro.js')
    assert html.index('/static/intro.js') < html.index('<body')
    assert any(attrs.get("src") == "/static/login.js" for attrs in scripts)
    assert _STATIC_RE.fullmatch("intro.js")
    assert _STATIC_RE.fullmatch("intro_art.js")
    assert _STATIC_TYPES["js"].startswith("text/javascript")


def test_static_form_stays_visible_and_focusable_without_javascript():
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    tags = LoginMarkup(html).tags
    card = next(attrs for tag, attrs in tags if tag == "main")
    assert card["class"] == "card login-card"
    assert not {"hidden", "inert", "aria-hidden", "style"} & card.keys()
    assert "intro-active" not in html
    for tag, attrs in tags:
        if tag in ("input", "button"):
            assert not {"disabled", "hidden", "inert", "tabindex"} & attrs.keys()
    css = (STATIC / "login.css").read_text(encoding="utf-8")
    assert ".intro-active .login-card" in css
    assert "visibility: hidden" in css
    js = (STATIC / "intro.js").read_text(encoding="utf-8")
    assert 'html.classList.add("intro-active")' in js
    assert 'card.setAttribute("aria-hidden", "true")' in js
    assert "card.inert = true" in js


def test_reduced_motion_and_failures_cannot_block_the_form():
    js = (STATIC / "intro.js").read_text(encoding="utf-8")
    assert '"(prefers-reduced-motion: reduce)"' in js
    assert 'typeof win.requestAnimationFrame !== "function"' in js
    assert "if (!ctx) { finish(); return; }" in js
    assert "catch (_) { finish(); }" in js
    assert "win.setTimeout(finish, 15200)" in js
    assert "canvas.remove()" in js
    assert "win.cancelAnimationFrame(raf)" in js
    assert "event.preventDefault(); event.stopImmediatePropagation();" in js


def test_login_assets_are_self_contained_and_have_no_inline_script_or_style():
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    for tag, attrs in LoginMarkup(html).tags:
        assert tag != "style"
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs)
        if tag == "script":
            assert attrs.get("src", "").startswith("/static/")
        for name in ("src", "href"):
            if name in attrs:
                assert attrs[name].startswith("/static/")
    for name in ("login.html", "login.css", "intro.js", "intro_art.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://|(?<!:)//[a-z0-9]+\.", source)
    assert all(not body.strip() for body in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))


def test_intro_card_and_canvas_fades_match_the_new_sequence():
    css = (STATIC / "login.css").read_text(encoding="utf-8")
    assert "background: #02030a" in css
    assert "transition: opacity 600ms ease, transform 600ms ease" in css
    assert "opacity: 0; transition: opacity 800ms ease" in css
    assert "transition-duration: 200ms" in css
    assert "animation: intro-card-rise 800ms ease both" in css
    assert "@keyframes intro-card-rise" in css
    assert ".intro-fast .login-card { animation: none; }" in css
    assert "prefers-reduced-motion: reduce" in css
