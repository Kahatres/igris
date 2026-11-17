"""Offline study UI contracts; behavioral JS coverage lives in study_browser.test.mjs."""

from pathlib import Path


STATIC = Path(__file__).resolve().parent.parent / "igris" / "static"


def test_missing_problem_has_accessible_recovery_and_hides_editor():
    html = (STATIC / "study.html").read_text(encoding="utf-8")
    js = (STATIC / "study.js").read_text(encoding="utf-8")
    assert 'id="prepare-problem"' in html
    assert 'id="practice-editor"' in html
    assert "response.status === 409" in js
    assert "error.status === 409" in js
    assert "setPracticeAvailable(false)" in js
    assert "await chooseStep(state.step)" in js


def test_chat_formatting_uses_dom_nodes_and_vendored_static_math():
    js = (STATIC / "study.js").read_text(encoding="utf-8")
    assert "formatMessage(node, text)" in js
    assert "innerHTML" not in js
    assert "createTextNode" in js
    assert "renderMathInElement" in js
    vendor = (STATIC / "vendor/mathlive/mathlive.min.mjs").read_text(encoding="utf-8")
    assert "as renderMathInElement" in vendor


def test_scene_fits_on_show_with_z_up_and_adaptive_geometry():
    js = (STATIC / "study_scene.js").read_text(encoding="utf-8")
    assert "export function sceneFrame" in js
    assert "fitCamera()" in js[js.index("show(scene)"):]
    assert "camera.up.set(0, 0, 1)" in js
    assert "camera.aspect" in js[js.index("function fitCamera"):]
    assert "frame.scale" in js and "frame.gridSize" in js


def test_desktop_bounds_panels_and_keeps_bottom_editor_visible():
    css = (STATIC / "study.css").read_text(encoding="utf-8")
    desktop = css.split("@media (min-width: 900px)")[1].split("@media")[0]
    assert "height: 100dvh" in desktop
    assert "overflow: hidden" in desktop
    assert "grid-template-rows: minmax(0,1fr) clamp(210px,31vh,280px)" in desktop
    assert ".plan-panel .panel-body, .solution-body" in desktop
    assert "overflow: auto" in desktop
    assert ".messages { min-height: 0; max-height: none" in desktop
    assert ".scene { flex: 1 1 0; min-height: 0; height: auto" in desktop
    assert ".solution-panel { grid-column: 1 / -1; }" in css


def test_phone_keeps_tabs_and_shrinkable_content_at_380_pixels():
    css = (STATIC / "study.css").read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 899px)")[1].split("@media")[0]
    assert "grid-template-columns: repeat(4,minmax(0,1fr))" in phone
    assert ".workspace { display: block" in phone
    assert '.workspace[data-active="chat"] .chat-panel' in phone
    for tab in ("plan", "scene", "solution"):
        assert f'.workspace[data-active="{tab}"]' in phone
    assert ".solution-heading { flex-wrap: wrap; }" in phone
    assert "min-width: 0; max-width: 100%" in phone
    assert "grid-template-columns: 24px minmax(0,1fr) auto" in css


def test_history_and_panel_scrolling_do_not_move_the_page():
    js = (STATIC / "study.js").read_text(encoding="utf-8")
    assert "await api('/api/study/history')" in js
    assert "path !== '/api/study/history'" in js
    assert "Promise.all([load(), loadHistory()])" in js
    assert "panel.scrollTop = panel.scrollHeight" in js
    assert "scrollIntoView" not in js
    assert "scrollTo(" not in js
    check = js.split("$('check').addEventListener")[1].split("$('explain').addEventListener")[0]
    assert "scrollChat()" in check
    assert ".focus(" not in check


def test_solution_edits_preserve_feedback_until_a_new_check():
    js = (STATIC / "study.js").read_text(encoding="utf-8")
    edit = js.split("function markEdited()")[1].split("function clearVerdict")[0]
    assert "state.revision++" in edit
    assert "explanation" not in edit and "verdict" not in edit
    assert "field.addEventListener('input', markEdited)" in js
    add_line = js.split("function addLine(")[1].split("const palette")[0]
    assert "clearVerdict" not in add_line
    palette = js.split("palette.forEach")[1].split("$('add-line')")[0]
    assert "clearVerdict" not in palette


def test_chat_progress_is_accessible_and_does_not_enable_fixtures():
    js = (STATIC / "study.js").read_text(encoding="utf-8")
    assert "function watchStudyProgress" in js
    assert "!path.startsWith('/api/study/progress')" in js
    assert "pending.setAttribute('aria-live', 'polite')" in js
    assert "pending.removeAttribute('aria-busy')" in js
    for text in ("Notlar okunuyor", "Plan kuruluyor", "Grafik hazırlanıyor"):
        assert text in js
