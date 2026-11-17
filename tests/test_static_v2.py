"""Static checks for the v2 (Zenith) web interface: igris/static/v2.html, v2.css, v2.js."""
import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "igris" / "static"
FILES = ["v2.html", "v2.css", "v2.js"]
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = {}
        self.tags = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            self.ids[a["id"]] = (tag, a)


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _parse():
    c = Collector()
    c.feed(_read("v2.html"))
    return c


def test_files_exist_and_are_served_names():
    for name in FILES:
        assert (STATIC / name).is_file(), name
    # web.py serves only flat lowercase .css/.js names from /static/
    for name in ("v2.css", "v2.js"):
        assert re.fullmatch(r"[a-z0-9_-]+\.(css|js)", name)


def test_page_links_its_own_assets_only():
    c = _parse()
    links = [a.get("href") for t, a in c.tags if t == "link"]
    scripts = [a.get("src") for t, a in c.tags if t == "script"]
    assert "/static/v2.css" in links
    assert scripts == ["/static/v2.js", "/static/pwa.js"]


def test_no_external_urls():
    for name in FILES:
        text = _read(name).replace(SVG_NAMESPACE, "")
        assert not re.search(r"https?://|//cdn|@import|url\((?!#|\"/static/)", text), name
    for _, a in _parse().tags:
        for k in ("src", "href"):
            v = a.get(k)
            if v:
                assert v.startswith(("/static/", "/", "#")) and not v.startswith("//"), v


def test_no_inner_html_or_eval():
    for name in ("v2.html", "v2.js"):
        text = _read(name)
        for bad in (".innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert bad not in text, (name, bad)
        assert not re.search(r"\beval\b|new Function", text), name


def test_required_ids_and_roles():
    ids = _parse().ids
    for i in ("scene", "scribbles", "state-line", "state-text", "wheel", "tip", "panel", "panel-title",
              "panel-body", "panel-close", "oracle", "oracle-log", "oracle-form", "oracle-input",
              "oracle-send", "oracle-hint", "motion-toggle", "clock"):
        assert i in ids, i
    assert ids["state-line"][1].get("role") == "status"
    assert ids["panel"][1].get("role") == "dialog"
    assert ids["oracle"][1].get("role") == "dialog"
    assert ids["oracle-input"][0] == "textarea"


def test_turkish_page_without_the_old_one():
    c = _parse()
    html = [a for t, a in c.tags if t == "html"][0]
    assert html.get("lang") == "tr"
    assert not any(t == "a" and a.get("href") in ("/", "/v2") for t, a in c.tags)
    assert any(t == "meta" and a.get("name") == "viewport" for t, a in c.tags)
    assert "Oracle" in _read("v2.html")
    # the old page is gone: nothing links to it any more
    for name in ("v2.html", "v2.js"):
        assert "klasik-eski" not in _read(name), name
    assert "Eski sayfa" not in _read("v2.html")


def test_js_uses_existing_api_paths():
    js = _read("v2.js")
    for p in ("/api/auth/me", "/api/state", "/api/history", "/api/ask", "/api/tasks",
              "/api/tasks/submit", "/api/tasks/cancel", "/api/tasks/resume", "/api/approvals",
              "/api/approvals/answer", "/api/inbox", "/api/multiagent/jobs", "/api/multiagent/cancel",
              "/api/files", "/api/files/upload", "/api/files/delete", "/api/models", "/api/roadmap",
              "/api/auth/logout", "/api/events", "/api/calendar", "/api/agenda",
              "/api/agenda/add", "/api/agenda/status"):
        assert '"' + p in js, p
    assert '"X-CSRF-Token"' in js


def test_every_state_has_a_text_equivalent():
    js = _read("v2.js")
    for state in ("boşta", "düşünüyor", "iş çalışıyor", "onay bekliyor", "hata"):
        assert '"' + state + '"' in js, state
    assert 'aria-live="polite"' in _read("v2.html")


def test_wheel_has_the_nine_modules():
    js = _read("v2.js")
    for name in ("Mail", "Görevler", "Oracle", "Dosyalar", "Multiagent", "Modeller",
                 "Yol haritası", "Görev Takvimi", "Hesap"):
        assert 'name: "' + name + '"' in js, name


def test_task_calendar_panel_has_branches_sections_and_kinds():
    js = _read("v2.js")
    assert 'title: "Görev Takvimi"' in js and 'name: "Takvim"' not in js
    for label in ("Kişisel", "Okul", "ESN", "Gecikmiş", "Bugün", "Bu hafta", "Sonra", "Bitti",
                  "Sınav", "Ödev", "Lab", "Sunum", "Yoklama", "Ödeme", "Toplantı", "Ders",
                  "Diğer"):
        assert '"' + label + '"' in js, label
    assert 'key: "agenda", path: "/api/agenda"' in js
    # the Google Calendar events stay under the task calendar
    assert "calEvents()" in js and "Google Takvim" in js


def test_owner_sentences_in_the_background():
    js = _read("v2.js")
    for line in ("Nobody cares.", "Everybody lies.", "Everything is a lie.", "Feelings are not real.",
                 "You should wake up.", "Igris is watching.", "Reality is a lie."):
        assert line in js, line


def test_motion_can_be_turned_off():
    css = _read("v2.css")
    assert "prefers-reduced-motion" in css
    assert 'html[data-motion="off"]' in css
    assert "max-width: 720px" in css  # phone layout


def test_glsl_avoids_reserved_names():
    js = _read("v2.js")
    names = set(re.findall(r"\b(gl_\w+)", js))
    assert names <= {"gl_FragCoord", "gl_FragColor", "gl_Position"}, names


def test_event_stream_is_only_a_hint_with_polling_fallback():
    js = _read("v2.js")
    assert "new EventSource(\"/api/events\")" in js
    assert "sseLive ? p.every * 6 : p.every" in js


def test_embedded_handwriting_font_with_its_licence():
    font = STATIC / "caveat.ttf"
    assert font.read_bytes()[:4] == b"\x00\x01\x00\x00"  # TrueType
    assert "SIL Open Font License" in _read("caveat-ofl.txt")
    css = _read("v2.css")
    assert 'url("/static/caveat.ttf")' in css and "Igris Hand" in css
    assert re.fullmatch(r"[a-z0-9_-]+\.(css|js|ttf)", "caveat.ttf")


def test_motion_choice_beats_the_system_preference():
    # The JS starts from prefers-reduced-motion and stores the owner's choice in data-motion;
    # a CSS reduced-motion rule must not switch animations off again after "Hareket: açık".
    css = _read("v2.css")
    for block in re.findall(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}", css, re.S):
        for selector in re.findall(r"([^{}]+)\{", block):
            for part in selector.split(","):
                assert ':not([data-motion="on"])' in part, part.strip()


def test_view_switch_has_odak_and_klasik_chips():
    c = _parse()
    ids = {a.get("id"): (tag, a) for tag, a in c.tags if a.get("id")}
    for key, label in (("odak", "Odak"), ("klasik", "Klasik")):
        tag, attrs = ids["view-" + key]
        assert tag == "button" and attrs.get("aria-pressed") in ("true", "false")
        assert attrs.get("data-view") == key
    assert ids["klasik-tabs"][1].get("role") == "tablist"
    text = _read("v2.html")
    assert ">Odak<" in text and ">Klasik<" in text


def test_view_switch_is_stored_on_the_server_or_locally():
    js = _read("v2.js")
    assert '"/api/prefs"' in js
    assert '"igris.v2.view"' in js  # localStorage when accounts are off
    assert 'setAttribute("data-view"' in js
    assert '"aria-pressed"' in js and '"aria-selected"' in js
    # Klasik reuses the module list and the panel renderers, it does not copy them
    assert "MODULES.forEach(function (m) {" in js and "PANELS[" in js


def test_klasik_default_tab_skips_navigating_modules():
    js = _read("v2.js")
    study = js.split('{ key: "study",', 1)[1].split('{ key: "core",', 1)[0]
    assert "navigates: true" in study
    first_tab = js.split("function firstTab() {", 1)[1].split("function applyView", 1)[0]
    assert 'return x.key !== "oracle" && !x.navigates && (!x.need || allowed(x.need));' in first_tab
    assert 'if (key === "study") { window.location.assign("/calis"); return; }' in js


def test_klasik_layout_and_labels_that_fit():
    css = _read("v2.css")
    assert 'html[data-view="klasik"] .panel' in css and 'html[data-view="klasik"] .stage' in css
    assert "max-width: 700px" in css  # tabs move to the top on narrow screens
    assert "text-overflow: ellipsis" in css
    js = _read("v2.js")
    assert "title: label" in js  # buttons carry their full text


def test_pages_are_installable_and_the_worker_never_touches_api_calls():
    static = Path(__file__).resolve().parent.parent / "igris" / "static"
    for page in ("v2.html", "login.html"):
        text = (static / page).read_text(encoding="utf-8")
        assert '<link rel="manifest" href="/static/manifest.webmanifest">' in text
        assert '<script src="/static/pwa.js"></script>' in text
    worker = (static / "sw.js").read_text(encoding="utf-8")
    assert 'mode !== "navigate"' in worker and "caches." not in worker
    assert "Igris'e ulaşılamıyor" in worker


def test_failed_sources_recover_after_a_server_restart():
    js = (Path(__file__).resolve().parent.parent / "igris" / "static" / "v2.js").read_text(encoding="utf-8")
    # the event stream's hello (a reconnect after a restart) refetches every failed source
    assert 'addEventListener("hello", function () { sseLive = true; refreshFailed(); })' in js
    # and a failed source retries soon instead of at its slow, stream-stretched pace
    assert "errors[p.key] ? Math.min(p.every, RETRY_MS)" in js


def test_agenda_shows_each_calendar_source_and_what_to_do():
    js = (Path(__file__).resolve().parent.parent / "igris" / "static" / "v2.js").read_text(encoding="utf-8")
    assert "data.agenda.sources" in js and "renderSources" in js
    for text in ("Moodle takvim linki girilmedi: python tools/set_keys.py moodle",
                 "ESN takvim kimliği tanımlı değil", "ESN takvimi bu Google hesabıyla paylaşılmamış",
                 "son eşitleme"):
        assert text in js


def test_agenda_panel_shows_the_google_sync_state():
    js = (STATIC / "v2.js").read_text(encoding="utf-8")
    assert "if (data.agenda && data.agenda.sync) { body.appendChild(syncLine(data.agenda.sync)); }" in js
    assert "Google Takvim ile eşitlendi · " in js and "Google izni gerekiyor" in js


def test_mail_panel_shows_decision_categories_with_filters():
    js = (Path(__file__).resolve().parent.parent / "igris" / "static" / "v2.js").read_text(encoding="utf-8")
    for name in ("Görev", "Güvenlik", "Bilgi", "Rutin", "Tanıtım"):
        assert f'"{name}"' in js
    assert "mailFilter = f[0]; renderPanel(true);" in js
    assert 'm.origin === "okul"' in js and "m.reason" in js
    # the wheel counts what needs the owner, not every risky-looking newsletter
    assert 'count: function () { return mailsIn("gorev").length + mailsIn("guvenlik").length; }' in js


def test_task_calendar_has_a_month_grid_view():
    js = (STATIC / "v2.js").read_text(encoding="utf-8")
    css = (STATIC / "v2.css").read_text(encoding="utf-8")
    assert '"/api/agenda/month?month="' in js and "renderMonth(body)" in js
    assert '[["liste", "Liste"], ["takvim", "Takvim"], ["hafta", "Hafta"], ["gun", "Gün"]]' in js
    assert 'role: "gridcell"' in js
    for word in ("Önceki ay", "Sonraki ay", "Bugün", "Bu günde kayıt yok."):
        assert word in js
    assert ".cal-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr));" in css
    # under 700 px the grid shows coloured dots instead of titles
    assert ".cal-ev { width: 7px; height: 7px;" in css


def test_account_panel_has_session_persona_and_password():
    js = _read("v2.js")
    for p in ("/api/session", "/api/persona", "/api/auth/password"):
        assert '"' + p + '"' in js, p
    for i in ("v2-session", "v2-session-new", "v2-persona", "v2-pass-current", "v2-pass-new",
              "v2-pass-repeat"):
        assert '"' + i + '"' in js, i
    for text in ("Yeni oturum", "Kişilik", "Parola değiştir", "Parolayı değiştir", "Yeni parolalar aynı değil"):
        assert text in js, text
    assert "current_password:" in js and 'window.location.replace("/login")' in js
    assert 'autocomplete: auto, maxlength: "256"' in js and '"current-password"' in js and '"new-password"' in js
    assert 'maxlength: "64"' in js
    # switching reuses the start-up history loader instead of a second one
    assert "reload: function () { loaded = false; load(); }" in js and "oracle.reload();" in js
    assert "Parola değiştir (klasik görünüm)" not in js


LOGIN_FILES = ["login.html", "login.js", "intro_art.js", "intro.js", "login.css"]


def test_login_files_load_nothing_from_outside():
    for name in FILES + LOGIN_FILES:
        assert (STATIC / name).is_file(), name
    for name in LOGIN_FILES:
        text = _read(name)
        assert not re.search(r"https?://|//cdn|@import|url\(", text), name
        for bad in (".innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert bad not in text, (name, bad)
        assert not re.search(r"\beval\b|new Function", text), name
    c = Collector()
    c.feed(_read("login.html"))
    for _, a in c.tags:
        for k in ("src", "href"):
            v = a.get(k)
            if v:
                assert v.startswith(("/static/", "#")), v
    html = [a for t, a in c.tags if t == "html"][0]
    assert html.get("lang") == "tr"
    assert any(t == "meta" and a.get("name") == "viewport" and "width=device-width" in a.get("content", "")
               for t, a in c.tags)
    scripts = [a.get("src") for t, a in c.tags if t == "script"]
    assert scripts == ["/static/intro_art.js", "/static/intro.js", "/static/login.js", "/static/pwa.js"]
    links = [a.get("href") for t, a in c.tags if t == "link" and a.get("rel") == "stylesheet"]
    assert links == ["/static/login.css"]


def test_no_page_loads_the_retired_files():
    for name in ("v2.html", "v2.js", "login.html", "login.js"):
        text = _read(name)
        for old in ("app.js", "app.css", "multiagent.js", "i18n.js", "index.html"):
            assert old not in text, (name, old)


def test_dom_helper_attaches_only_function_handlers():
    js = _read("v2.js")
    # on* attributes become listeners only when they are functions; strings are never set
    assert 'k.slice(0, 2) === "on" && typeof v === "function"' in js


def test_mail_module_shows_why_a_mail_may_not_reach_the_phone():
    js = (STATIC / "v2.js").read_text(encoding="utf-8")
    for text in ('"bildirim kanalı ayarlı değil"', '"bildirim gönderilemiyor"',
                 '" uyarı bekliyor"', '"model mailleri okuyamıyor"',
                 '"çok fazla mail birikti, eskileri okunamadı"'):
        assert text in js
    assert 'st.channel === "none"' in js and 'st.model === "down"' in js
    assert "inboxProblems().length > 0" in js  # the wheel marks the module
    assert 'note("Dikkat: " + problems.join(" · "))' in js

def test_agenda_items_with_an_end_show_the_end_day_when_it_differs():
    js = _read("v2.js")
    body = js.split("function agendaWhen(it)", 1)[1].split("function nextAgenda", 1)[0]
    assert "it.start" in body and "it.due" in body
    assert "agendaDay(b) === agendaDay(a)" in body

def test_month_grid_shows_an_item_past_midnight_on_every_day_it_covers():
    # source checks only: the suite runs no JavaScript engine (AGENTS.md: no real CLI)
    js = _read("v2.js")
    span = js.split("function agendaSpanDays(startIso, dueIso)", 1)[1].split("function monthEntries", 1)[0]
    # only items timed on both ends are split; others are placed as before
    assert "startIso.length <= 10 || dueIso.length <= 10) { return []; }" in span
    # an end at exactly 00:00 belongs to the day before
    assert "var midnightEnd = b.getTime() === endDay.getTime();" in span
    assert "endDay.getDate() - 1) : endDay;" in span
    # start day runs to 00:00, a middle day is all day, the last day starts at 00:00
    assert 'var from = first ? hhmm(a) : "00:00", to = last && !midnightEnd ? hhmm(b) : "00:00";' in span
    assert '"tüm gün"' in span and "n < 42" in span
    entries = js.split("function monthEntries(d)", 1)[1].split("function renderMonth", 1)[0]
    # one record: every part carries the same item object and its done class; later days say "(devam)"
    assert "var parts = agendaSpanDays(it.start, it.due);" in entries
    assert 'title: p.cont ? "(devam) " + it.title : it.title, item: it,' in entries
    assert 'cls: cls + (p.cont ? " cal-cont" : ""), part: p' in entries
    # the day list says which hours of the day a part covers
    assert '"Bu gün: " + e.part.when' in js and 'picked.forEach(function (e) { body.appendChild(entryCard(e)); });' in js
    css = _read("v2.css")
    assert ".cal-ev.cal-cont { background: transparent;" in css
    # a day holding the end of one item and the start of the next lists them in time order
    assert '(a.at || a.when || "00:00") < (b.at || b.when || "00:00")' in entries


def _between(text, start, end):
    assert start in text, start
    return text.split(start, 1)[1].split(end, 1)[0]


def test_week_and_day_timelines_place_items_on_an_hour_axis():
    # source checks only: the suite runs no JavaScript engine (AGENTS.md: no real CLI)
    js = _read("v2.js")
    # the chosen view survives a reload, month view stays one of the choices
    assert 'return v === "takvim" || v === "hafta" || v === "gun" ? v : "liste";' in js
    assert 'if (agendaView === "hafta" || agendaView === "gun") { renderTimeline(body, agendaView);' in js
    # an item is a [start, end) range; a due-only item is a marker at its due time; all-day is not timed
    rng = _between(js, "function itemRange(startIso, dueIso)", "function daySlice")
    assert "if (s === null && u === null) { return null; }" in rng
    assert "if (s === null) { return { a: u, b: u + 1800000, marker: true }; }" in rng
    assert "return { a: s, b: u !== null && u > s ? u : s + 1800000, marker: false };" in rng
    # a day shows only its own part; before/after mark a part that continues across midnight
    sl = _between(js, "function daySlice(a, b, from, to)", "function layoutLanes")
    assert "if (b <= from || a >= to) { return null; }" in sl
    assert "before: a < from, after: b > to" in sl
    # overlapping items share the column side by side
    lanes = _between(js, "function layoutLanes(slices)", "function coversDay")
    assert "while (lane < ends.length && ends[lane] > s.top) { lane++; }" in lanes
    assert "slices[i].lanes = ends.length;" in lanes
    assert "function coversDay(first, lastExclusive, day) { return first <= day && day < lastExclusive; }" in js
    assert 'minutes >= 1440 ? "24:00"' in js
    # the same sources as the month grid, with no Google copy of an Igris item
    entries = _between(js, "function timelineEntries(d)", "function renderTimeline")
    assert "googleEvents(d).forEach" in entries and "itemRange(it.start, it.due)" in entries
    tl = _between(js, "function renderTimeline(body, mode)", "function renderAgenda")
    # a week starts on Monday and is read from the six-week month payload
    assert 'var first = mode === "hafta" ? mondayOf(focus) : focus;' in tl
    assert "var key = monthKey(mondayOf(first));" in tl and "loadMonth(key)" in tl
    assert "return addDays(date, -((date.getDay() + 6) % 7));" in js
    for word in ("Önceki ", "Sonraki ", "Bugün", "tüm gün", "Ayrıntı ve işlemler için bir kayda dokun."):
        assert word in tl
    # a continued part says so; blocks are placed by minutes on the axis
    assert '(s.before ? "(devam) " : "") + range' in tl
    assert 'style: "top: " + s.top / 60 * TL_HOUR + "px; height: " + height + "px; left: calc("' in tl
    assert '" tl-before" : ""' in tl and '" tl-after" : ""' in tl and '" tl-marker" : ""' in tl
    # today is highlighted and has a current-time line; the view opens at about 07:00
    assert '(day === today ? " tl-today" : "")' in tl and 'class: "tl-now"' in tl
    assert "wrap.scrollTop = 7 * TL_HOUR;" in tl
    # a day header in the week and a picked day in the month open the day view
    assert "onclick: function () { openDay(day); }" in tl
    assert "if (calDay === picked) { openDay(picked); return; }" in js
    assert 'function openDay(day) { calFocus = day; tlPicked = null; tlScroll = null; setAgendaView("gun"); }' in js
    # tapping a block shows the same card the month grid shows
    assert "entryCard({ title: picked.entry.title, item: picked.entry.item" in tl
    css = _read("v2.css")
    assert "grid-template-columns: 44px repeat(var(--tl-cols), minmax(92px, 1fr));" in css
    assert ".tl-axis, .tl-col { position: relative; height: 1056px; }" in css  # 24 x TL_HOUR
    assert "var TL_HOUR = 44;" in js
    assert ".tl-corner, .tl-dayhead { position: sticky; top: 0;" in css
    for cls in (".tl-block {", ".tl-before {", ".tl-after {", ".tl-marker {", ".tl-now {", ".tl-chip {", ".tl-col.tl-today"):
        assert cls in css
    # timeline elements never take .cal-ev, which a phone shrinks to a dot
    assert "tl-block cal-ev" not in js and "tl-chip cal-ev" not in js


def test_multiagent_panel_has_no_obsolete_classic_handoff():
    js = _read("v2.js")
    panel = js.split('multiagent: { title:', 1)[1].split('models: { title:', 1)[0]
    assert 'd.max_parallel + " iş."' in panel
    assert "klasik görünümde" not in panel
    assert 'href: "/"' not in panel


def test_password_errors_are_translated_with_a_turkish_fallback():
    panel = _read("v2.js").split("function passwordCard() {", 1)[1].split("function taskCard", 1)[0]
    assert '"Invalid current password": "Mevcut parola yanlış."' in panel
    assert ('"Password must contain 12 to 256 characters": '
            '"Yeni parola 12 ile 256 karakter arasında olmalı."') in panel
    assert 'errors[err.message] || "Parola değiştirilemedi. Lütfen tekrar dene."' in panel
    assert "status.textContent = err.message;" not in panel


def test_retired_forge_has_no_module_panel_poll_or_event_dependency():
    js = _read("v2.js")
    assert '"/api/forge"' not in js
    assert 'key: "forge"' not in js
    assert 'forge: {' not in js
    assert 'forge: ["forge"]' not in js
    assert 'FORGE_' not in js


def test_core_panel_and_study_module_are_wired():
    js = _read("v2.js")
    assert 'name: "Çalışma"' in js and '"/calis"' in js
    assert 'name: "Core"' in js
    for path in ("/api/core", "/api/core/add", "/api/core/doc?id=", "/api/core/page"):
        assert path in js
    assert 'method: "DELETE"' in js
    assert "Görselden okundu" in js
    study = _read("study.js")
    assert "X-CSRF-Token" in study and "/api/auth/me" in study


def test_no_forge_leftovers_in_ui_or_roadmap():
    js = (STATIC / "v2.js").read_text(encoding="utf-8").lower()
    assert "fluent" not in js and "forge →" not in js
    roadmap = (STATIC.parent / "roadmap.json").read_text(encoding="utf-8")
    assert "kaldırıldı" in roadmap.split("summarizer.py", 1)[1].split('"', 1)[0]
    assert "kaldırıldı" in roadmap.split("/api/multiagent/live", 1)[1].split('"', 1)[0]


def test_approval_summary_keeps_mail_line_breaks():
    js, css = _read("v2.js"), _read("v2.css")
    assert 'h("strong", { class: "approval-summary", text: a.summary || a.action })' in js
    rule = re.search(r"\.card \.approval-summary \{([^}]*)\}", css)
    assert rule and "white-space: pre-wrap" in rule.group(1)
