/* Igris v2 · Zenith. A neon module wheel around the prism eye, a scribbled background,
   glass panels per module and the Oracle chat overlay. It uses only the existing JSON API,
   builds every node with createElement (no innerHTML) and loads nothing from outside. */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var $ = function (id) { return document.getElementById(id); };
  var csrf = "";
  var me = { enabled: false, user: null };

  // ---------- DOM helpers ----------
  function put(el, attrs, kids) {
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) { return; }
      if (k === "text") { el.textContent = v; }
      else if (k.slice(0, 2) === "on" && typeof v === "function") { el.addEventListener(k.slice(2), v); }
      else { el.setAttribute(k, v === true ? "" : String(v)); }
    });
    [].concat(kids || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) { return; }
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }
  function h(tag, attrs, kids) { return put(document.createElement(tag), attrs, kids); }
  function s(tag, attrs, kids) { return put(document.createElementNS(SVGNS, tag), attrs, kids); }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function hhmm(date) { return date.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" }); }
  function isoClock(iso) { return typeof iso === "string" ? iso.slice(11, 16) : ""; }
  function fileSize(n) {
    if (n < 1024) { return n + " B"; }
    if (n < 1048576) { return (n / 1024).toLocaleString("tr-TR", { maximumFractionDigits: 1 }) + " KB"; }
    return (n / 1048576).toLocaleString("tr-TR", { maximumFractionDigits: 1 }) + " MB";
  }

  // ---------- HTTP ----------
  function api(path, body) {
    var opts = { method: "GET", credentials: "same-origin" };
    if (body !== undefined) {
      opts = { method: "POST", credentials: "same-origin", body: JSON.stringify(body),
               headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf } };
    }
    return fetch(path, opts).then(function (resp) {
      if (resp.status === 401) { window.location.replace("/login"); }
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var err = new Error(data.error || ("HTTP " + resp.status));
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    }, function () {
      var err = new Error("Sunucuya ulaşılamıyor");
      err.status = 0;
      throw err;
    });
  }

  // ---------- motion ----------
  var MOTION_KEY = "igris.v2.motion";
  function readMotion() {
    try { var v = localStorage.getItem(MOTION_KEY); if (v) { return v === "on"; } } catch (e) { /* storage blocked */ }
    try { return !window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return true; }
  }
  var motion = readMotion();
  function setMotion(on) {
    motion = on;
    document.documentElement.setAttribute("data-motion", on ? "on" : "off");
    $("motion-toggle").textContent = "Hareket: " + (on ? "açık" : "kapalı");
    try { localStorage.setItem(MOTION_KEY, on ? "on" : "off"); } catch (e) { /* storage blocked */ }
    scene.kick();
  }

  // ---------- store: polls the existing API and tells the views ----------
  var data = {};
  var errors = {};
  var asking = false;
  var sseLive = false;
  var RETRY_MS = 15000;
  var listeners = [];
  function changed() { listeners.forEach(function (fn) { fn(); }); }

  // need: "account" = only with accounts on; "admin" = admin, or a local server without accounts
  var POLLS = [
    { key: "state", path: "/api/state", every: 30000 },
    { key: "tasks", path: "/api/tasks", every: 5000, need: "account" },
    { key: "approvals", path: "/api/approvals", every: 8000, need: "account" },
    { key: "inbox", path: "/api/inbox", every: 60000, need: "admin" },
    { key: "jobs", path: "/api/multiagent/jobs", every: 10000, need: "admin" },
    { key: "files", path: "/api/files", every: 30000, need: "account" },
    { key: "core", path: "/api/core", every: 5000, need: "admin" },
    { key: "calendar", path: "/api/calendar", every: 300000, need: "admin" },
    { key: "agenda", path: "/api/agenda", every: 60000, need: "account" },
    { key: "models", path: "/api/models", every: 120000 },
    { key: "roadmap", path: "/api/roadmap", every: 600000 }
  ];
  function allowed(need) {
    if (!need) { return true; }
    if (need === "account") { return !!me.user; }
    return !me.user || me.user.role === "admin";
  }
  function poll(p) {
    if (!allowed(p.need)) { return; }
    // while the event stream is live it tells us what changed; polling is only a fallback
    // a failed source is asked again soon (RETRY_MS), whatever its normal pace
    var next = function () {
      var wait = errors[p.key] ? Math.min(p.every, RETRY_MS) : (sseLive ? p.every * 6 : p.every);
      setTimeout(function () { poll(p); }, wait);
    };
    if (document.visibilityState === "hidden") { next(); return; }
    fetchInto(p).then(next);
  }
  function fetchInto(p) {
    return api(p.path).then(function (d) {
      data[p.key] = d;
      delete errors[p.key];
    }, function (err) {
      if (err.status === 404 || err.status === 403) { data[p.key] = null; delete errors[p.key]; }
      else { errors[p.key] = err.message; }
    }).then(changed);
  }
  function refresh(key) {
    var p = POLLS.filter(function (x) { return x.key === key; })[0];
    if (!p || !allowed(p.need)) { return Promise.resolve(); }
    return fetchInto(p);
  }
  // after the server comes back (a restart), clear every error at once instead of
  // waiting for each source's own slow poll
  function refreshFailed() { Object.keys(errors).forEach(function (k) { refresh(k); }); }

  // ---------- derived facts ----------
  function list(key, field) { var d = data[key]; return d && Array.isArray(d[field]) ? d[field] : []; }
  function tasksBy(states) { return list("tasks", "tasks").filter(function (t) { return states.indexOf(t.status) >= 0; }); }
  function jobsBy(states) { return list("jobs", "jobs").filter(function (j) { return states.indexOf(j.status) >= 0; }); }
  function riskyMails() { return list("inbox", "mails").filter(function (m) { return m.risk === "high" || m.risk === "medium"; }); }
  // the decision step's categories; old rows without one count as "bilgi"
  var MAIL_CATS = [["gorev", "Görev"], ["guvenlik", "Güvenlik"], ["bilgi", "Bilgi"], ["rutin", "Rutin"], ["tanitim", "Tanıtım"]];
  var MAIL_CAT_CLASS = { gorev: "risk-medium", guvenlik: "risk-high" };
  var mailFilter = "hepsi";
  function mailCategory(m) { return m.category || "bilgi"; }
  function mailsIn(cat) { return list("inbox", "mails").filter(function (m) { return mailCategory(m) === cat; }); }
  function inboxState() { var d = data.inbox; return d && d.status ? d.status.state : null; }
  // what keeps a mail from reaching the owner: the channel, waiting alerts, the model, Gmail
  function inboxProblems() {
    var st = data.inbox && data.inbox.status, out = [];
    if (!st) { return out; }
    if (st.channel === "none") { out.push("bildirim kanalı ayarlı değil"); }
    if (st.channel === "failing") { out.push("bildirim gönderilemiyor"); }
    if (st.pending_alerts) { out.push(st.pending_alerts + " uyarı bekliyor"); }
    if (st.model === "down") { out.push("model mailleri okuyamıyor"); }
    if (st.state === "error") { out.push("Gmail okunamıyor"); }
    if (st.backlog_truncated) { out.push("çok fazla mail birikti, eskileri okunamadı"); }
    return out;
  }
  function calEvents() { var d = data.calendar; return d && d.status === "ok" ? d.events : []; }
  function eventStart(e) { return new Date(e.all_day ? e.start + "T00:00:00" : e.start); }
  function soonEvents(hours) {
    var now = Date.now(), until = now + hours * 3600000;
    return calEvents().filter(function (e) {
      var t = eventStart(e).getTime();
      return t <= until && (e.all_day ? new Date(e.end + "T00:00:00").getTime() > now : t >= now - 3600000);
    });
  }
  function eventWhen(e) {
    var d = eventStart(e);
    var day = d.toLocaleDateString("tr-TR", { weekday: "short", day: "numeric", month: "short" });
    return e.all_day ? day + " · tüm gün" : day + " · " + hhmm(d);
  }
  var CAL_STATES = { disabled: "takvim bu sunucuda kapalı", not_configured: "Google girişi yok",
    needs_consent: "takvim izni bekleniyor", auth_down: "Google bağlantısı düştü", error: "takvim okunamadı" };
  // ---------- task calendar (Görev Takvimi) ----------
  var BRANCHES = [["kisisel", "Kişisel"], ["okul", "Okul"], ["esn", "ESN"]];
  var AGENDA_SECTIONS = [["overdue", "Gecikmiş"], ["today", "Bugün"], ["week", "Bu hafta"], ["later", "Sonra"]];
  var AGENDA_KINDS = { exam: "Sınav", assignment: "Ödev", lab: "Lab", presentation: "Sunum",
    attendance: "Yoklama", payment: "Ödeme", meeting: "Toplantı", lesson: "Ders", study: "Çalışma", other: "Diğer" };
  var agendaBranch = "okul";
  function agendaSection(name) {
    var d = data.agenda, out = [];
    if (!d || !d.branches) { return out; }
    BRANCHES.forEach(function (b) { out = out.concat((d.branches[b[0]] || {})[name] || []); });
    return out;
  }
  function agendaWhenValue(it) { return it.due || it.start || ""; }
  function agendaDate(it) {
    var v = agendaWhenValue(it);
    return new Date(v.length === 10 ? v + "T00:00:00" : v);
  }
  function agendaDay(d) { return d.toLocaleDateString("tr-TR", { weekday: "short", day: "numeric", month: "short" }); }
  function agendaWhen(it) {
    var s = it.start || "", u = it.due || "";
    if (s.length > 10 && u.length > 10 && new Date(u) > new Date(s)) {
      // a timed item with an end: show the start, and the end day when it is another day
      var a = new Date(s), b = new Date(u);
      return agendaDay(a) + " · " + hhmm(a) + "–" + (agendaDay(b) === agendaDay(a) ? "" : agendaDay(b) + " · ") + hhmm(b);
    }
    var d = agendaDate(it);
    return agendaWhenValue(it).length === 10 ? agendaDay(d) : agendaDay(d) + " · " + hhmm(d);
  }
  function nextAgenda() {
    var open = agendaSection("today").concat(agendaSection("week"), agendaSection("later"));
    open.sort(function (a, b) { return agendaDate(a) - agendaDate(b); });
    return open[0] || null;
  }
  // "2026-11-12" + "18:30" -> "2026-11-12T18:30:00+03:00" in the browser's time zone
  function localIso(day, clock) {
    var d = new Date(day + "T" + clock + ":00");
    var off = -d.getTimezoneOffset(), sign = off < 0 ? "-" : "+", abs = Math.abs(off);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return day + "T" + clock + ":00" + sign + pad(Math.floor(abs / 60)) + ":" + pad(abs % 60);
  }
  function waitingCount() { return list("approvals", "approvals").length + tasksBy(["waiting_for_user"]).length; }

  function mood() {
    var down = errors.state || Object.keys(errors).length >= 2;
    if (down) { return { key: "hata", text: "hata · " + (errors.state || "bağlantı sorunlu") }; }
    if (inboxState() === "auth_down") { return { key: "hata", text: "hata · Gmail bağlantısı düştü" }; }
    var wait = waitingCount();
    if (wait) { return { key: "onay bekliyor", text: "onay bekliyor · " + wait + " karar senden" }; }
    if (asking) { return { key: "düşünüyor", text: "düşünüyor · Oracle cevap yazıyor" }; }
    var tasks = tasksBy(["queued", "running", "waiting_for_event"]).length;
    var jobs = jobsBy(["queued", "running"]).length;
    if (tasks || jobs) {
      var parts = [];
      if (tasks) { parts.push(tasks + " görev"); }
      if (jobs) { parts.push(jobs + " multiagent işi"); }
      return { key: "iş çalışıyor", text: "iş çalışıyor · " + parts.join(", ") };
    }
    return { key: "boşta", text: "boşta · her şey sakin" };
  }

  // ---------- states: eye colours, pupil size and scene uniforms ----------
  var MOODS = {
    "boşta": { a: "#5dffd0", b: "#2fff6a", rx: 9, ry: 30, pulse: 0, glitch: 0, orbit: .35, colA: [.02, .35, .30], colB: [.20, 1.0, .70] },
    "düşünüyor": { a: "#7fd7ff", b: "#5d8bff", rx: 6, ry: 34, pulse: 0, glitch: 0, orbit: 1.4, colA: [.05, .35, .6], colB: [.3, .8, 1.0] },
    "iş çalışıyor": { a: "#ffd36a", b: "#ff8a2f", rx: 12, ry: 26, pulse: 0, glitch: 0, orbit: 2.6, colA: [.45, .22, .02], colB: [1.0, .7, .2] },
    "onay bekliyor": { a: "#ff7ae6", b: "#c43cff", rx: 16, ry: 22, pulse: 2.0, glitch: 0, orbit: .6, colA: [.35, .02, .35], colB: [1.0, .3, .9] },
    "hata": { a: "#ff5a6e", b: "#ff2f3f", rx: 4, ry: 36, pulse: 0, glitch: .9, orbit: 1.0, colA: [.4, .02, .06], colB: [1.0, .2, .3] }
  };

  // ---------- scene: WebGL light rays, pulse rings and orbiting sparks ----------
  var scene = (function () {
    var canvas = $("scene");
    var gl = null;
    try { gl = canvas.getContext("webgl", { antialias: false, powerPreference: "low-power" }); } catch (e) { gl = null; }
    var target = MOODS["boşta"];
    var cur = { pulse: 0, orbit: .35, colA: target.colA.slice(), colB: target.colB.slice() };
    var glitchUntil = 0;
    var running = false;
    if (!gl) {
      canvas.hidden = true;
      document.body.style.background = "radial-gradient(circle at 50% 50%, #0b2a20 0%, #000 45%)";
      return { set: function () {}, kick: function () {} };
    }
    var FS = [
      "precision mediump float;",
      "uniform vec2 uRes; uniform float uTime, uPulse, uGlitch, uOrbit; uniform vec3 uColA, uColB;",
      "float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }",
      "void main(){",
      "  vec2 uv = (gl_FragCoord.xy - .5 * uRes) / min(uRes.x, uRes.y);",
      "  float band = step(.93, hash(vec2(floor(uv.y * 38.), floor(uTime * 18.))));",
      "  uv.x += uGlitch * band * .035 * (hash(vec2(uTime, uv.y)) - .5);",
      "  float r = length(uv), ang = atan(uv.y, uv.x);",
      "  vec3 col = vec3(0.);",
      "  float rays = pow(max(sin(ang * 48. + hash(vec2(floor(ang * 48. / 6.2831 * 7.), 1.)) * 6.), 0.), 30.);",
      "  col += mix(uColA, uColB, .5 + .5 * sin(ang * 3. + uTime * .2)) * rays * .35 * (1. - smoothstep(.12, .29, r)) * smoothstep(0., .16, r);",
      "  col += mix(uColA, uColB, .6) * exp(-r * 9.) * .35;",
      "  float ph = fract(uTime * uPulse * .25);",
      "  col += uColB * (1. - smoothstep(0., .006, abs(r - (.2 + ph * .32)))) * (1. - ph) * step(.01, uPulse) * .9;",
      "  for (int i = 0; i < 36; i++) {",
      "    float fi = float(i); float a = fi * 2.399 + uTime * uOrbit * (.15 + hash(vec2(fi, 1.)) * .35);",
      "    float rad = .19 + hash(vec2(fi, 2.)) * .07; float inc = (hash(vec2(fi, 3.)) - .5) * 1.2;",
      "    vec3 q = vec3(cos(a) * rad, sin(a) * rad * inc, sin(a) * rad);",
      "    vec2 sp = q.xy / (1. + q.z * .8); float sz = .0022 * (1.2 - q.z * 2.);",
      "    col += mix(uColA, uColB, hash(vec2(fi, 4.))) * (1. - smoothstep(0., max(sz, .0005), length(uv - sp))) * (q.z < 0. ? .4 : 1.3);",
      "  }",
      "  col = col / (1. + col * .6);",
      "  gl_FragColor = vec4(pow(col, vec3(.9)), 1.);",
      "}"].join("\n");
    var VS = "attribute vec2 p; void main(){ gl_Position = vec4(p, 0., 1.); }";
    function shader(type, src) {
      var sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { throw new Error(gl.getShaderInfoLog(sh)); }
      return sh;
    }
    var prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    var U = {};
    ["uRes", "uTime", "uPulse", "uGlitch", "uOrbit", "uColA", "uColB"].forEach(function (n) { U[n] = gl.getUniformLocation(prog, n); });
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5) * (window.innerWidth < 720 ? .6 : .8);
    function resize() {
      canvas.width = Math.round(window.innerWidth * dpr);
      canvas.height = Math.round(window.innerHeight * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
      draw(performance.now());
    }
    var t0 = performance.now();
    function draw(now) {
      var k = motion ? .04 : 1;
      cur.pulse += (target.pulse - cur.pulse) * k;
      cur.orbit += (target.orbit - cur.orbit) * k;
      for (var i = 0; i < 3; i++) {
        cur.colA[i] += (target.colA[i] - cur.colA[i]) * k;
        cur.colB[i] += (target.colB[i] - cur.colB[i]) * k;
      }
      var glitch = motion && now < glitchUntil ? target.glitch * (glitchUntil - now) / 2000 : 0;
      gl.uniform2f(U.uRes, canvas.width, canvas.height);
      gl.uniform1f(U.uTime, motion ? (now - t0) / 1000 : 0);
      gl.uniform1f(U.uPulse, motion ? cur.pulse : 0);
      gl.uniform1f(U.uOrbit, cur.orbit);
      gl.uniform1f(U.uGlitch, glitch);
      gl.uniform3fv(U.uColA, cur.colA);
      gl.uniform3fv(U.uColB, cur.colB);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    function frame(now) {
      if (!motion) { running = false; draw(now); return; }
      requestAnimationFrame(frame);
      if (!document.hidden) { draw(now); }
    }
    function kick() {
      if (motion && !running) { running = true; requestAnimationFrame(frame); }
      if (!motion) { draw(performance.now()); }
    }
    window.addEventListener("resize", resize);
    resize();
    return {
      set: function (m) {
        if (target === m) { return; }
        target = m;
        if (m.glitch) { glitchUntil = performance.now() + 2000; }
        kick();
      },
      kick: kick
    };
  })();

  // ---------- the prism eye (an original character) ----------
  var eye = (function () {
    var stopA = s("stop", { offset: "0", "stop-color": "#5dffd0" });
    var stopB = s("stop", { offset: "1", "stop-color": "#2fff6a" });
    var lid = s("path", { d: "M-62,0 Q0,-48 62,0 Q0,48 -62,0 Z", stroke: "#5dffd0", "stroke-width": "4.5" });
    var pupil = s("ellipse", { id: "pupil", cx: "0", cy: "0", rx: "9", ry: "30", fill: "#5dffd0" });
    var dots = s("circle", { r: "252", fill: "none", stroke: "#2fff6a", "stroke-width": "1.4", "stroke-dasharray": "2 14",
      opacity: ".55", class: "rot" });
    var group = s("g", { id: "eye", filter: "url(#glow)", fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round",
      role: "button", tabindex: "0", "aria-label": "Igris'in gözü: bekleyen kararları aç" }, [
      s("g", { stroke: "url(#prism)", "stroke-width": "5" }, [
        s("path", { d: "M0,-215 L150,0 L0,215 L-150,0 Z" }),
        s("path", { d: "M0,-215 L55,0 L0,215 M0,-215 L-55,0 L0,215", "stroke-width": "2.5", opacity: ".75" }),
        s("path", { d: "M-150,0 L-62,0 M62,0 L150,0", "stroke-width": "2.5", opacity: ".6" }),
        s("path", { d: "M-102,-68 L102,-68 M-102,68 L102,68", "stroke-width": "1.6", opacity: ".35" }),
        s("g", { class: "facet", opacity: "0", stroke: "none" }, [
          s("path", { d: "M0,-215 L55,0 L0,0 Z", fill: "#5dffd0", opacity: ".2" }),
          s("path", { d: "M0,215 L-55,0 L0,0 Z", fill: "#2fff6a", opacity: ".2" })
        ])
      ]),
      lid, pupil
    ]);
    var gradient = s("linearGradient", { id: "prism", x1: "0", x2: "0", y1: "0", y2: "1" }, [stopA, stopB]);
    function look(x, y) {
      var b = $("wheel").getBoundingClientRect();
      var dx = x - (b.left + b.width / 2), dy = y - (b.top + b.height / 2);
      var d = Math.hypot(dx, dy) || 1, k = Math.min(d / 300, 1);
      pupil.setAttribute("transform", "translate(" + (dx / d * 34 * k).toFixed(1) + "," + (dy / d * 14 * k).toFixed(1) + ")");
    }
    window.addEventListener("pointermove", function (e) { look(e.clientX, e.clientY); });
    setInterval(function () {
      if (!motion || document.hidden) { return; }
      lid.setAttribute("d", "M-62,0 Q0,-4 62,0 Q0,4 -62,0 Z");
      pupil.style.opacity = "0";
      setTimeout(function () { lid.setAttribute("d", "M-62,0 Q0,-48 62,0 Q0,48 -62,0 Z"); pupil.style.opacity = "1"; }, 140);
    }, 5200);
    return {
      nodes: [gradient, group, dots], group: group,
      set: function (key) {
        var m = MOODS[key];
        stopA.setAttribute("stop-color", m.a);
        stopB.setAttribute("stop-color", m.b);
        lid.setAttribute("stroke", m.a);
        dots.setAttribute("stroke", m.b);
        pupil.setAttribute("fill", m.a);
        pupil.setAttribute("rx", m.rx);
        pupil.setAttribute("ry", m.ry);
        $("wheel").classList.toggle("busy-eye", key === "iş çalışıyor" || key === "düşünüyor");
        document.documentElement.style.setProperty("--eye-a", m.a);
        document.documentElement.style.setProperty("--eye-b", m.b);
      }
    };
  })();

  // ---------- modules on the wheel ----------
  var MODULES = [
    { key: "study", name: "Çalışma", need: "admin", navigates: true,
      glyph: "M-32,-24 h28 v48 h-28 z M4,-24 h28 v48 h-28 z M0,-24 v48",
      info: function () { return "Sınav planı · sahne · çözüm kontrolü"; } },
    { key: "core", name: "Core", need: "admin",
      glyph: "M-30,-24 h60 v48 h-60 z M-18,-12 h36 M-18,0 h36 M-18,12 h24",
      info: function () { return list("core", "documents").length + " ders belgesi"; },
      count: function () { return list("core", "documents").length; } },
    { key: "mail", name: "Mail", need: "admin",
      glyph: "M-34,-22 h68 v44 h-68 z M-34,-22 L0,6 L34,-22",
      info: function () {
        var st = inboxState();
        if (data.inbox === undefined) { return "yükleniyor"; }
        if (!data.inbox || st === "disabled") { return "gelen kutusu izleme kapalı"; }
        if (st === "not_configured") { return "Gmail bağlı değil"; }
        if (st === "auth_down") { return "Gmail bağlantısı düştü"; }
        var problems = inboxProblems();
        if (problems.length) { return problems.join(" · ") + " · son kontrol " + isoClock(data.inbox.status.last_poll); }
        var tasks = mailsIn("gorev").length, alarms = mailsIn("guvenlik").length;
        return tasks + " görev · " + (alarms ? alarms + " güvenlik uyarısı" : "güvenlik sorunu yok") +
          " · son kontrol " + isoClock(data.inbox.status.last_poll);
      },
      count: function () { return mailsIn("gorev").length + mailsIn("guvenlik").length; },
      alert: function () { return inboxState() === "auth_down" || inboxProblems().length > 0 || mailsIn("guvenlik").length > 0; } },
    { key: "tasks", name: "Görevler", need: "account",
      glyph: "M-30,-26 h40 v28 h-40 z M-14,-10 h40 v28 h-40 z M2,6 h40 v28 h-40 z",
      info: function () {
        if (!me.user) { return "görevler hesap açıkken çalışır"; }
        var run = tasksBy(["running"]).length, q = tasksBy(["queued"]).length, w = tasksBy(["waiting_for_user"]).length;
        if (!run && !q && !w) { return "bekleyen görev yok"; }
        return [run && run + " çalışıyor", q && q + " sırada", w && w + " yanıt bekliyor"].filter(Boolean).join(" · ");
      },
      count: function () { return tasksBy(["queued", "running", "waiting_for_user", "waiting_for_event"]).length; },
      busy: function () { return tasksBy(["running"]).length > 0; },
      alert: function () { return tasksBy(["waiting_for_user"]).length > 0; } },
    { key: "oracle", name: "Oracle",
      glyph: "M-36,-24 h72 v40 h-40 l-18,16 v-16 h-14 z M-20,-4 h40",
      info: function () { return asking ? "cevap yazıyor" : "sohbet · Ctrl + Boşluk"; },
      busy: function () { return asking; } },
    { key: "files", name: "Dosyalar", need: "account",
      glyph: "M-36,-22 h24 l8,8 h40 v40 h-72 z",
      info: function () {
        if (!me.user) { return "dosyalar hesap açıkken çalışır"; }
        var d = data.files;
        return d ? d.files.length + " dosya · " + fileSize(d.used_bytes) + " kullanılıyor" : "yükleniyor";
      },
      count: function () { return list("files", "files").length; } },
    { key: "multiagent", name: "Multiagent", need: "admin",
      glyph: "M0,-26 m-8,0 a8,8 0 1,0 16,0 a8,8 0 1,0 -16,0 M-28,22 m-8,0 a8,8 0 1,0 16,0 a8,8 0 1,0 -16,0 M28,22 m-8,0 a8,8 0 1,0 16,0 a8,8 0 1,0 -16,0 M-4,-19 L-24,15 M4,-19 L24,15 M-20,22 H20",
      info: function () {
        var d = data.jobs;
        if (!d) { return d === null ? "multiagent kapalı" : "yükleniyor"; }
        var run = jobsBy(["running"]).length, q = jobsBy(["queued"]).length;
        return run || q ? run + " çalışıyor · " + q + " sırada" : "aktif iş yok";
      },
      count: function () { return jobsBy(["queued", "running"]).length; },
      busy: function () { return jobsBy(["running"]).length > 0; } },
    { key: "models", name: "Modeller",
      glyph: "M-24,-24 h48 v48 h-48 z M-12,-12 h24 v24 h-24 z M-24,-12 h-12 M-24,12 h-12 M24,-12 h12 M24,12 h12 M-12,-24 v-12 M12,-24 v-12 M-12,24 v12 M12,24 v12",
      info: function () {
        var p = list("models", "providers");
        var ma = data.models && data.models.multiagent;
        var active = ma && Array.isArray(ma.isciler)
          ? ma.isciler.filter(function (w) { return w.durum === "aktif"; }).length : null;
        if (active !== null) { return active + " multiagent işçisi aktif · Igris " + p.length + " model"; }
        return p.length ? p.length + " sağlayıcı · birincil " + p[0].name : "yükleniyor";
      } },
    { key: "roadmap", name: "Yol haritası",
      glyph: "M-40,24 C-20,24 -24,-10 0,-10 S20,-24 40,-24 M-40,24 m-5,0 a5,5 0 1,0 10,0 a5,5 0 1,0 -10,0 M40,-24 l-10,-6 M40,-24 l-6,10",
      info: function () {
        var plans = list("roadmap", "plans").filter(function (p) { return p.status === "in_progress"; });
        return plans.length ? plans.length + " plan sürüyor · " + plans[0].title : "yenilikler ve planlar";
      } },
    { key: "calendar", name: "Görev Takvimi",
      glyph: "M-34,-26 h68 v56 h-68 z M-34,-12 h68 M-18,-34 v14 M18,-34 v14 M-20,2 h10 M6,2 h10 M-20,16 h10",
      info: function () {
        var late = agendaSection("overdue").length;
        var item = nextAgenda();
        if (item) { return (late ? late + " gecikmiş · " : "") + "sıradaki: " + item.title + " · " + agendaWhen(item); }
        if (late) { return late + " gecikmiş görev"; }
        if (!allowed("admin")) { return data.agenda === undefined ? "yükleniyor" : "yaklaşan görev yok"; }
        var d = data.calendar;
        if (d === undefined) { return "yükleniyor"; }
        if (!d || d.status !== "ok") { return CAL_STATES[d ? d.status : "disabled"] || "takvim okunamadı"; }
        var next = calEvents()[0];
        return next ? "sıradaki: " + next.title + " · " + eventWhen(next) : "önümüzdeki 14 gün boş";
      },
      count: function () { return agendaSection("overdue").length + agendaSection("today").length + soonEvents(48).length; },
      alert: function () { return agendaSection("overdue").length > 0 || soonEvents(24).length > 0; } },
    { key: "account", name: "Hesap",
      glyph: "M0,-12 m-14,0 a14,14 0 1,0 28,0 a14,14 0 1,0 -28,0 M-32,32 C-28,10 28,10 32,32",
      info: function () {
        if (!me.user) { return "yerel kullanım · hesap sistemi kapalı"; }
        return me.user.display_name + " · " + (me.user.role === "admin" ? "yönetici" : "kullanıcı");
      } }
  ];

  function offText(m) { return m.need === "account" ? "hesap sistemi kapalı" : "yalnızca yönetici"; }

  var R1 = 330, R2 = 440;
  var segs = {};
  function point(r, a) { return (r * Math.cos(a)).toFixed(1) + "," + (r * Math.sin(a)).toFixed(1); }
  function buildWheel() {
    var wheel = $("wheel");
    wheel.appendChild(s("defs", {}, [
      s("linearGradient", { id: "outer", x1: "0", x2: "1", y1: "0", y2: "0" }, [
        s("stop", { offset: "0", "stop-color": "#ff7a2f" }), s("stop", { offset: ".35", "stop-color": "#ff2f6e" }),
        s("stop", { offset: ".6", "stop-color": "#e02fd8" }), s("stop", { offset: "1", "stop-color": "#6a3cff" })]),
      s("linearGradient", { id: "inner", x1: "0", x2: "1", y1: "0", y2: "0" }, [
        s("stop", { offset: "0", "stop-color": "#ff3f3f" }), s("stop", { offset: ".5", "stop-color": "#ff2fc0" }),
        s("stop", { offset: "1", "stop-color": "#a93cff" })]),
      s("filter", { id: "glow", x: "-50%", y: "-50%", width: "200%", height: "200%" }, [
        s("feGaussianBlur", { stdDeviation: "4", result: "b" }),
        s("feMerge", {}, [s("feMergeNode", { in: "b" }), s("feMergeNode", { in: "b" }), s("feMergeNode", { in: "SourceGraphic" })])]),
      eye.nodes[0]
    ]));
    wheel.appendChild(eye.nodes[1]);
    wheel.appendChild(eye.nodes[2]);
    var ring = s("g", { class: "rot" }, [
      s("circle", { r: R2, stroke: "#2fff6a", "stroke-width": "6" }),
      s("circle", { r: R1, stroke: "#2fff6a", "stroke-width": "5" })
    ]);
    var n = MODULES.length;
    MODULES.forEach(function (m, i) {
      var a0 = (i / n) * Math.PI * 2 - Math.PI / 2, a1 = ((i + 1) / n) * Math.PI * 2 - Math.PI / 2, am = (a0 + a1) / 2;
      var deg = (am * 180 / Math.PI + 90).toFixed(1);
      var start = point(R1, a0).split(","), end = point(R2, a0).split(",");
      var glyph = s("path", { class: "glyph", d: m.glyph, stroke: "#2fdc66", "stroke-width": "3.2" });
      var count = s("text", { class: "count", "text-anchor": "middle", x: "0", y: "-46" });
      var g = s("g", { class: "seg", role: "button", tabindex: "0", "aria-label": m.name }, [
        s("path", { class: "hit", stroke: "none", d: "M" + point(R1, a0) + " L" + point(R2, a0) + " A" + R2 + "," + R2 +
          " 0 0,1 " + point(R2, a1) + " L" + point(R1, a1) + " A" + R1 + "," + R1 + " 0 0,0 " + point(R1, a0) + " z" }),
        s("line", { x1: start[0], y1: start[1], x2: end[0], y2: end[1], stroke: "#2fff6a", "stroke-width": "5" }),
        s("g", { transform: "translate(" + point((R1 + R2) / 2, am) + ") rotate(" + deg + ")" }, [glyph, count])
      ]);
      g.addEventListener("pointermove", function (e) { showTip(m, e.clientX, e.clientY); });
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("focus", function () { var b = g.getBoundingClientRect(); showTip(m, b.right, b.top); });
      g.addEventListener("blur", hideTip);
      g.addEventListener("click", function () { hideTip(); open(m.key); });
      g.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(m.key); }
      });
      segs[m.key] = { g: g, glyph: glyph, count: count };
      ring.appendChild(g);
    });
    wheel.appendChild(s("g", { filter: "url(#glow)", fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" }, [
      s("circle", { r: "472", stroke: "url(#outer)", "stroke-width": "7" }),
      ring,
      s("circle", { r: "318", stroke: "url(#inner)", "stroke-width": "6" })
    ]));
    eye.group.addEventListener("click", function () { open("waiting"); });
    eye.group.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open("waiting"); }
    });
  }
  function renderWheel() {
    MODULES.forEach(function (m) {
      var seg = segs[m.key];
      var off = m.need && !allowed(m.need);
      var c = m.count && !off ? m.count() : 0;
      seg.count.textContent = c ? String(c) : "";
      seg.g.classList.toggle("busy", !off && !!(m.busy && m.busy()));
      seg.g.classList.toggle("alert", !off && !!(m.alert && m.alert()));
      seg.g.setAttribute("opacity", off ? ".35" : "1");
      seg.g.setAttribute("aria-label", m.name + ": " + (off ? offText(m) : m.info()));
      seg.glyph.setAttribute("stroke", seg.g.classList.contains("busy") ? "#5dffa0" : "#2fdc66");
    });
  }

  // ---------- tooltip ----------
  var tipFor = null;
  function showTip(m, x, y) {
    var tip = $("tip");
    tipFor = m;
    clear(tip);
    var off = m.need && !allowed(m.need);
    put(tip, {}, [h("b", { text: m.name }), off ? offText(m) : m.info(), h("br"), h("small", { text: "tıkla: aç" })]);
    tip.hidden = false;
    var w = tip.offsetWidth, hgt = tip.offsetHeight;
    tip.style.left = Math.min(x + 18, window.innerWidth - w - 8) + "px";
    tip.style.top = Math.min(y + 12, window.innerHeight - hgt - 8) + "px";
  }
  function hideTip() { tipFor = null; $("tip").hidden = true; }

  // ---------- scribbled background ----------
  var LINES = ["Nobody cares.", "Everybody lies.", "Everything is a lie.", "Feelings are not real.",
    "You should wake up.", "Igris is watching.", "Reality is a lie."];
  var SMALL = ["Re = ρVc / μ", "CL(α) ≈ 2π(α − α₀)", "Cp = (p − p∞) / ½ρV²", "∮ p·n dA", "∂u/∂t + u·∇u",
    "LJULV ELU VLVWHP", "ZDNH XS", "0x2F7A", "sin α · cos α", "▷ ▢ ◇ ⊙", "look closer", "it watches",
    "NOBODY CARES", "wake up", "011 0110 1", "inbox → plan → done", "D = (cos α ; sin α)"];
  var lastLive = "";
  function liveLines() {
    var out = [];
    if (data.inbox && data.inbox.status && data.inbox.status.state === "ok") {
      out.push("gelen kutusu: " + riskyMails().length + " riskli");
      out.push("son kontrol " + isoClock(data.inbox.status.last_poll));
    }
    if (data.tasks) { out.push("görev: " + tasksBy(["queued", "running"]).length + " açık"); }
    if (data.approvals) { out.push("onay: " + list("approvals", "approvals").length); }
    if (data.jobs) { out.push("multiagent: " + jobsBy(["running"]).length + " koşu"); }
    var soon = soonEvents(24);
    if (soon.length) { out.push("bugün: " + soon[0].title); }
    if (data.state && data.state.version) { out.push("igris v" + data.state.version); }
    return out;
  }
  function buildScribbles() {
    var box = $("scribbles");
    clear(box);
    var seed = 7;
    var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    var live = liveLines();
    var W = window.innerWidth, H = window.innerHeight;
    var cols = Math.max(3, Math.round(W / 230)), rows = Math.max(6, Math.round(H / 70));
    var fonts = ["", "caps", "print"];
    var k = 0, li = 0;
    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        var x = (c + rnd() * .7) / cols * 100, y = (r + rnd() * .6) / rows * 100;
        var centre = Math.hypot((x - 50) / 50, (y - 50) / 50) < .55;
        var big = k % 3 === 0, isLive = !big && !centre && li < live.length && k % 4 === 1;
        k += 1;
        var text = big ? LINES[Math.floor(rnd() * LINES.length)]
          : (isLive ? live[li++] : SMALL[Math.floor(rnd() * SMALL.length)]);
        var cls = "n " + (big ? "big " : "") + (isLive ? "live print" : fonts[Math.floor(rnd() * 3)]);
        // English scribbles keep English capitals ("IGRIS", not the Turkish dotted "İ")
        var el = h("div", { class: cls, lang: isLive ? "tr" : "en" });
        if (!isLive && rnd() < .12) { el.appendChild(h("s", { text: text })); } else { el.textContent = text; }
        el.style.left = x + "%";
        el.style.top = y + "%";
        if (!big) { el.style.fontSize = (13 + rnd() * 9).toFixed(0) + "px"; }
        el.style.transform = "translate(-50%,-50%) rotate(" + (rnd() * 16 - 8).toFixed(1) + "deg)";
        if (!isLive) { el.style.setProperty("--o", (centre ? .16 + rnd() * .12 : .34 + rnd() * .36).toFixed(2)); }
        el.style.color = "hsl(" + ((x * 2.6 + 20) % 360).toFixed(0) + ",100%,62%)";
        box.appendChild(el);
      }
    }
    lastLive = live.join("|");
  }

  // ---------- panels ----------
  var openKey = null;
  function card(kids) { return h("div", { class: "card" }, kids); }
  function note(text) { return h("p", { class: "muted small", text: text }); }
  function button(label, onclick, cls) { return h("button", { type: "button", class: "btn " + (cls || ""), text: label, title: label, onclick: onclick }); }

  var TASK_STATES = { queued: "Sırada", running: "Çalışıyor", done: "Tamamlandı", failed: "Başarısız",
    cancelled: "İptal edildi", cancel_requested: "İptal bekliyor", interrupted: "Sunucu durduğu için yarım kaldı",
    waiting_for_user: "Yanıtını bekliyor", waiting_for_event: "Bir olayı bekliyor" };
  var JOB_STATES = { queued: "Sırada", running: "Çalışıyor", done: "Bitti", failed: "Başarısız", cancelled: "İptal edildi" };
  var RISKS = { high: "Yüksek risk", medium: "Dikkat", low: "Güvenli" };
  var ROAD = { done: "Tamamlandı", in_progress: "Sürüyor", planned: "Planlandı" };
  var taskResults = {};

  function agendaCard(it) {
    var row = h("div", { class: "row" });
    var done = button("Bitti", function () {
      done.disabled = true;
      api("/api/agenda/status", { id: it.id, status: "done" }).then(function () { return refresh("agenda"); },
        function (err) { done.disabled = false; row.appendChild(note("Kaydedilemedi: " + err.message)); });
    }, "quiet");
    row.appendChild(done);
    return card([h("strong", { text: it.title, title: it.title }),
      h("p", {}, [h("span", { class: "tag", text: AGENDA_KINDS[it.kind] || it.kind }),
        h("span", { class: "muted small", text: agendaWhen(it) + (it.location ? " · " + it.location : "") })]), row]);
  }
  function agendaForm(body) {
    var pick = function (label, pairs, value) {
      return h("select", { "aria-label": label }, pairs.map(function (p) {
        return h("option", { value: p[0], selected: p[0] === value, text: p[1] });
      }));
    };
    var branch = pick("Dal", BRANCHES, agendaBranch);
    var kind = pick("Tür", Object.keys(AGENDA_KINDS).map(function (k) { return [k, AGENDA_KINDS[k]]; }), "assignment");
    var title = h("input", { type: "text", maxlength: "200", placeholder: "Ne var?", "aria-label": "Başlık" });
    var day = h("input", { type: "date", "aria-label": "Tarih" });
    var clock = h("input", { type: "time", "aria-label": "Saat (isteğe bağlı)" });
    var status = note("");
    var add = button("Ekle", function () {
      if (!title.value.trim()) { title.focus(); return; }
      if (!day.value) { day.focus(); return; }
      var entry = { branch: branch.value, kind: kind.value, title: title.value.trim(),
        due: clock.value ? localIso(day.value, clock.value) : day.value, all_day: !clock.value };
      add.disabled = true;
      api("/api/agenda/add", entry).then(function () {
        agendaBranch = entry.branch;
        title.value = "";
        status.textContent = "Eklendi.";
        return refresh("agenda").then(function () { renderPanel(true); });
      }, function (err) { status.textContent = "Eklenemedi: " + err.message; }).then(function () { add.disabled = false; });
    });
    body.appendChild(h("form", { class: "agenda-add", "aria-label": "Görev ekle",
      onsubmit: function (ev) { ev.preventDefault(); add.click(); } }, [branch, kind, title, day, clock, add]));
    body.appendChild(status);
  }
  // calendar sources the server syncs into the owner's agenda (Moodle, shared ESN calendar)
  var SOURCE_NAMES = { moodle: "Moodle", esn: "ESN takvimi" };
  var SOURCE_HELP = {
    no_link: "Moodle takvim linki girilmedi: python tools/set_keys.py moodle",
    no_calendar_id: "ESN takvim kimliği tanımlı değil: IGRIS_ESN_CALENDAR_ID",
    no_google: "Google'a giriş yapılmamış: python tools/gmail_auth.py login",
    not_shared: "ESN takvimi bu Google hesabıyla paylaşılmamış",
    needs_consent: "Google girişi takvim iznini içermiyor: python tools/gmail_auth.py login",
    auth_down: "Google bağlantısı düştü: python tools/gmail_auth.py login",
    no_owner: "Yönetici hesabı bulunamadı",
    error: "okunamadı"
  };
  function sourceLine(name, s) {
    var label = SOURCE_NAMES[name] || name;
    if (s.state === "ok") {
      var when = s.last_sync ? hhmm(new Date(s.last_sync * 1000)) : "-";
      return { cls: null, text: label + ": " + s.count + " kayıt · son eşitleme " + when };
    }
    if (s.state === "starting") { return { cls: null, text: label + ": ilk eşitleme bekleniyor" }; }
    var help = SOURCE_HELP[s.reason] || SOURCE_HELP.error;
    if (s.reason === "error" && s.error) { help += ": " + s.error; }
    return { cls: s.state === "error" ? "risk-high" : null, text: label + ": " + help };
  }
  function renderSources(body, sources) {
    var names = Object.keys(sources || {});
    if (!names.length) { return; }
    body.appendChild(h("div", { class: "agenda-sources", role: "status", "aria-label": "Takvim kaynakları" },
      names.map(function (n) {
        var line = sourceLine(n, sources[n]);
        return h("p", { class: "small " + (line.cls || "muted"), text: line.text, title: line.text });
      })));
  }
  // the owner's agenda is mirrored to Google Calendar ("Igris · Okul" and the others)
  var SYNC_TEXT = {
    starting: "Google Takvim eşitleniyor…",
    not_configured: "Google'a giriş yapılmamış. Terminalde: python tools/gmail_auth.py login",
    needs_consent: "Google izni gerekiyor. Terminalde bir kez: python tools/gmail_auth.py login",
    auth_down: "Google bağlantısı düştü. Terminalde yeniden giriş yap: python tools/gmail_auth.py login"
  };
  function syncLine(s) {
    var ok = s.state === "ok";
    var text = ok ? "Google Takvim ile eşitlendi · " + (s.last_sync ? hhmm(new Date(s.last_sync)) : "")
      : SYNC_TEXT[s.state] || ("Google Takvim eşitlenemedi" + (s.error ? ": " + s.error : "."));
    return h("p", { class: "small " + (ok ? "muted" : "risk-high"), role: "status", text: text, title: text });
  }
  // ---------- the month view: a six-week grid of agenda items and Google events ----------
  var AGENDA_VIEW_KEY = "igris.v2.agendaView";
  var agendaView = (function () {
    try {
      var v = localStorage.getItem(AGENDA_VIEW_KEY);
      return v === "takvim" || v === "hafta" || v === "gun" ? v : "liste";
    } catch (e) { return "liste"; }
  })();
  var calMonth = (function () { var d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); })();
  var calDay = null;          // "YYYY-MM-DD" the owner picked in the grid
  var monthData = null;       // { key, data, agenda } from /api/agenda/month
  var monthLoading = null;
  var WEEKDAYS = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"];
  var BRANCH_NAMES = { kisisel: "Kişisel", okul: "Okul", esn: "ESN" };
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function ymd(d) { return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()); }
  function monthKey(d) { return d.getFullYear() + "-" + pad2(d.getMonth() + 1); }
  function dayOf(value) { return value.length === 10 ? value : ymd(new Date(value)); }
  function setAgendaView(v) {
    agendaView = v;
    try { localStorage.setItem(AGENDA_VIEW_KEY, v); } catch (e) { /* storage blocked */ }
    renderPanel(true);
  }
  function loadMonth(key) {
    if (monthLoading === key) { return; }
    monthLoading = key;
    var agendaNow = JSON.stringify(data.agenda && data.agenda.branches);
    api("/api/agenda/month?month=" + key).then(function (d) {
      monthData = { key: key, data: d, agenda: agendaNow };
    }, function (err) {
      monthData = { key: key, data: null, error: err.message, agenda: agendaNow };
    }).then(function () { monthLoading = null; if (openKey === "calendar") { renderPanel(true); } });
  }
  // The days a timed item with a start and an end covers, for the month grid; [] when the
  // item is not timed on both ends (it is placed as before). Each part: { day, when, at, cont }
  // where "when" is the clock range shown, "at" the sort key and "cont" marks a following day.
  // An end at exactly 00:00 belongs to the day before, so it never spills into the next day.
  function agendaSpanDays(startIso, dueIso) {
    if (!startIso || !dueIso || startIso.length <= 10 || dueIso.length <= 10) { return []; }
    var a = new Date(startIso), b = new Date(dueIso);
    if (!(b > a)) { return []; }
    var day = new Date(a.getFullYear(), a.getMonth(), a.getDate());
    var endDay = new Date(b.getFullYear(), b.getMonth(), b.getDate());
    var midnightEnd = b.getTime() === endDay.getTime();
    var lastDay = midnightEnd ? new Date(endDay.getFullYear(), endDay.getMonth(), endDay.getDate() - 1) : endDay;
    if (lastDay.getTime() === day.getTime() && !midnightEnd) { return []; }  // a same-day item
    var parts = [];
    for (var n = 0; day <= lastDay && n < 42; n++) {
      var first = n === 0, last = day.getTime() === lastDay.getTime();
      var from = first ? hhmm(a) : "00:00", to = last && !midnightEnd ? hhmm(b) : "00:00";
      var whole = !first && (!last || midnightEnd);
      parts.push({ day: ymd(day), when: whole ? "tüm gün" : from + "–" + to, at: whole ? "00:00" : from, cont: !first });
      day = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1);
    }
    return parts;
  }
  // Google events of a month payload, without an event that is an Igris item's own copy
  // (its id is the item's calendar_event_id), so nothing is shown twice
  function googleEvents(d) {
    var g = d.google, own = {};
    (d.items || []).forEach(function (it) { if (it.calendar_event_id) { own[it.calendar_event_id] = true; } });
    return ((g && g.status === "ok" && g.events) || []).filter(function (e) { return !own[e.id]; });
  }
  // day -> [{ when, title, cls, item?, event? }]: agenda items on their due (else start) day,
  // an item that runs past midnight on every day it covers (one record, the same item object
  // in each part), Google events on every day they cover
  function monthEntries(d) {
    var days = {};
    var add = function (day, entry) { (days[day] = days[day] || []).push(entry); };
    (d.items || []).forEach(function (it) {
      var cls = "cal-" + it.branch + (it.status === "done" ? " cal-done" : "");
      var parts = agendaSpanDays(it.start, it.due);
      if (parts.length) {
        parts.forEach(function (p) {
          // "(devam)" goes first: a narrow cell cuts the end of the title off
          add(p.day, { when: p.when, at: p.at, title: p.cont ? "(devam) " + it.title : it.title, item: it,
            cls: cls + (p.cont ? " cal-cont" : ""), part: p });
        });
        return;
      }
      var v = agendaWhenValue(it);
      add(dayOf(v), { when: v.length === 10 ? "" : hhmm(new Date(v)), title: it.title, item: it, cls: cls });
    });
    googleEvents(d).forEach(function (e) {
      if (!e.all_day) { add(dayOf(e.start), { when: hhmm(new Date(e.start)), title: e.title, event: e, cls: "cal-google" }); return; }
      var day = new Date(e.start + "T00:00:00"), last = new Date((e.end || e.start) + "T00:00:00");
      if (last <= day) { last = new Date(day.getTime() + 86400000); }
      for (var n = 0; day < last && n < 42; n++) {
        add(ymd(day), { when: "", title: e.title, event: e, cls: "cal-google" });
        day = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1);
      }
    });
    Object.keys(days).forEach(function (k) {
      days[k].sort(function (a, b) { return (a.at || a.when || "00:00") < (b.at || b.when || "00:00") ? -1 : 1; });
    });
    return days;
  }
  function renderMonth(body) {
    var key = monthKey(calMonth);
    var title = calMonth.toLocaleDateString("tr-TR", { month: "long", year: "numeric" });
    var shift = function (n) {
      calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + n, 1);
      calDay = null;
      renderPanel(true);
    };
    body.appendChild(h("div", { class: "cal-head" }, [
      h("button", { type: "button", class: "btn quiet", text: "‹", title: "Önceki ay", "aria-label": "Önceki ay", onclick: function () { shift(-1); } }),
      h("strong", { class: "cal-title", text: title, "aria-live": "polite" }),
      h("button", { type: "button", class: "btn quiet", text: "›", title: "Sonraki ay", "aria-label": "Sonraki ay", onclick: function () { shift(1); } }),
      button("Bugün", function () {
        var now = new Date();
        calMonth = new Date(now.getFullYear(), now.getMonth(), 1);
        calDay = ymd(now);
        renderPanel(true);
      }, "quiet")]));
    body.appendChild(h("p", { class: "cal-legend small muted" }, [
      h("span", { class: "cal-dot cal-kisisel" }), "Kişisel ", h("span", { class: "cal-dot cal-okul" }), "Okul ",
      h("span", { class: "cal-dot cal-esn" }), "ESN ", h("span", { class: "cal-dot cal-google" }), "Google Takvim"]));
    var stale = !monthData || monthData.key !== key ||
      monthData.agenda !== JSON.stringify(data.agenda && data.agenda.branches);
    if (stale) { loadMonth(key); }
    if (!monthData || monthData.key !== key) { body.appendChild(note("Yükleniyor…")); return; }
    var d = monthData.data;
    if (!d) { body.appendChild(note("Takvim okunamadı: " + (monthData.error || ""))); return; }
    if (d.google && d.google.status !== "ok" && d.google.status !== "disabled") {
      body.appendChild(note("Google Takvim etkinlikleri okunamadı (" + d.google.status + ")."));
    }
    var days = monthEntries(d);
    var today = ymd(new Date());
    var inMonth = function (day) { return day.slice(0, 7) === key; };
    if (!calDay || !inMonth(calDay)) { calDay = inMonth(today) ? today : key + "-01"; }
    var grid = h("div", { class: "cal-grid", role: "grid", "aria-label": title });
    WEEKDAYS.forEach(function (w) { grid.appendChild(h("div", { class: "cal-wd", role: "columnheader", text: w })); });
    var start = new Date(d.from + "T00:00:00");
    for (var i = 0; i < 42; i++) {
      var date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      var day = ymd(date), list = days[day] || [];
      var label = date.toLocaleDateString("tr-TR", { day: "numeric", month: "long", weekday: "long" }) +
        (list.length ? ", " + list.length + " kayıt" : "");
      var cell = h("button", { type: "button", role: "gridcell", "aria-label": label, title: label,
        "aria-pressed": day === calDay ? "true" : "false",
        class: "cal-day" + (inMonth(day) ? "" : " cal-out") + (day === today ? " cal-today" : ""),
        // a click picks the day; a click on the picked day opens it hour by hour
        onclick: (function (picked) { return function () {
          if (calDay === picked) { openDay(picked); return; }
          calDay = picked; renderPanel(true);
        }; })(day) },
        [h("span", { class: "cal-num", text: String(date.getDate()) })]);
      list.slice(0, 3).forEach(function (e) {
        // the title fits a narrow cell better than the clock; the clock is in the tooltip and the day list
        // a Moodle course code ("KnF52029: ") would fill the cell, so the cell drops it
        var short = e.item ? e.title.replace(/^((?:\(devam\) )?)[A-Za-z]+\d+:\s*/, "$1") : e.title;
        cell.appendChild(h("span", { class: "cal-ev " + e.cls, text: short || e.title, title: (e.when ? e.when + " " : "") + e.title }));
      });
      if (list.length > 3) { cell.appendChild(h("span", { class: "cal-more", text: "+" + (list.length - 3) })); }
      grid.appendChild(cell);
    }
    body.appendChild(grid);
    var picked = days[calDay] || [];
    body.appendChild(h("h3", { class: "cal-dayhead" }, [new Date(calDay + "T00:00:00").toLocaleDateString("tr-TR",
      { weekday: "long", day: "numeric", month: "long" }), " ", button("Saat saat", function () { openDay(calDay); }, "quiet")]));
    if (!picked.length) { body.appendChild(note("Bu günde kayıt yok.")); }
    picked.forEach(function (e) { body.appendChild(entryCard(e)); });
  }
  // The card for one calendar entry (the month day list and the timeline detail): the item's
  // own card with its actions, or a read-only card for done items and Google events. A part of
  // an item past midnight also says which hours of this day it covers.
  function entryCard(e) {
    var c;
    if (e.item && (e.item.status === "added" || e.item.status === "proposed")) {
      c = agendaCard(e.item);
      c.classList.add("cal-card", e.cls.split(" ")[0]);
    } else {
      var tag = e.event ? "Google Takvim" : (BRANCH_NAMES[e.item.branch] || e.item.branch) + " · bitti";
      c = card([h("strong", { class: e.item ? "cal-struck" : null, text: e.title, title: e.title }),
        h("p", {}, [h("span", { class: "tag", text: tag }),
          h("span", { class: "muted small", text: e.event ? eventWhen(e.event) : agendaWhen(e.item) })])]);
    }
    if (e.part) {
      c.appendChild(h("p", { class: "muted small", text: "Bu gün: " + e.part.when + (e.part.cont ? " (devam)" : "") }));
    }
    return c;
  }

  // ---------- the week and day timelines: an hour axis 00:00-24:00, one column per day ----------
  var TL_HOUR = 44;             // pixels per hour
  var TL_MIN_BLOCK = 20;        // a short block stays tall enough to read and to tap
  var calFocus = null;          // "YYYY-MM-DD" the week/day timeline shows
  var tlPicked = null;          // key of the block whose card is shown under the timeline
  var tlScroll = null;          // the timeline's scroll position, kept across re-renders
  function dayStart(day) { return new Date(day + "T00:00:00"); }
  function addDays(date, n) { return new Date(date.getFullYear(), date.getMonth(), date.getDate() + n); }
  function mondayOf(date) { return addDays(date, -((date.getDay() + 6) % 7)); }
  function openDay(day) { calFocus = day; tlPicked = null; tlScroll = null; setAgendaView("gun"); }
  // Pure: the [a, b) of an agenda item in ms, or null for an all-day item. A due-only item is a
  // marker at its due time; a single moment gets 30 minutes so it can be drawn.
  function itemRange(startIso, dueIso) {
    var s = startIso && startIso.length > 10 ? new Date(startIso).getTime() : null;
    var u = dueIso && dueIso.length > 10 ? new Date(dueIso).getTime() : null;
    if (s === null && u === null) { return null; }
    if (s === null) { return { a: u, b: u + 1800000, marker: true }; }
    return { a: s, b: u !== null && u > s ? u : s + 1800000, marker: false };
  }
  // Pure: the part of [a, b) that falls on the day starting at the local midnight ``from``
  // (ms) and ending at ``to``, as minutes from that midnight; before/after mark a part that
  // continues from the day before or into the next day. null when nothing falls on the day.
  function daySlice(a, b, from, to) {
    if (b <= from || a >= to) { return null; }
    var top = Math.max(a, from), bottom = Math.min(b, to);
    return { top: (top - from) / 60000, bottom: (bottom - from) / 60000, before: a < from, after: b > to };
  }
  // Pure: side-by-side lanes for overlapping slices of one day. Each slice gets lane (0-based)
  // and lanes (how many share its overlapping group), so its width is 1/lanes of the column.
  function layoutLanes(slices) {
    var order = slices.map(function (s, i) { return i; }).sort(function (x, y) {
      return slices[x].top - slices[y].top || slices[y].bottom - slices[x].bottom;
    });
    var group = [], ends = [], groupEnd = -1;
    var close = function () { group.forEach(function (i) { slices[i].lanes = ends.length; }); group = []; ends = []; };
    order.forEach(function (i) {
      var s = slices[i];
      if (group.length && s.top >= groupEnd) { close(); groupEnd = -1; }
      var lane = 0;
      while (lane < ends.length && ends[lane] > s.top) { lane++; }
      ends[lane] = s.bottom;
      s.lane = lane;
      group.push(i);
      groupEnd = Math.max(groupEnd, s.bottom);
    });
    close();
    return slices;
  }
  // Pure: true when an all-day range of dates [first, lastExclusive) covers ``day`` (all
  // "YYYY-MM-DD"; such strings compare in date order).
  function coversDay(first, lastExclusive, day) { return first <= day && day < lastExclusive; }
  function clockLabel(minutes) { return minutes >= 1440 ? "24:00" : pad2(Math.floor(minutes / 60)) + ":" + pad2(Math.round(minutes % 60)); }
  // the timed entries and all-day entries of a month payload, one object per record
  function timelineEntries(d) {
    var timed = [], allDay = [];
    (d.items || []).forEach(function (it) {
      var cls = "cal-" + it.branch + (it.status === "done" ? " cal-done" : "");
      var r = itemRange(it.start, it.due);
      if (r) { timed.push({ a: r.a, b: r.b, marker: r.marker, title: it.title, item: it, cls: cls, key: "i" + it.id }); return; }
      var first = (it.start || it.due).slice(0, 10), last = (it.due || it.start).slice(0, 10);
      if (last < first) { last = first; }
      allDay.push({ first: first, end: ymd(addDays(dayStart(last), 1)), title: it.title, item: it, cls: cls, key: "i" + it.id });
    });
    googleEvents(d).forEach(function (e) {
      if (!e.all_day) {
        var a = new Date(e.start).getTime(), b = e.end ? new Date(e.end).getTime() : a + 1800000;
        timed.push({ a: a, b: b > a ? b : a + 1800000, title: e.title, event: e, cls: "cal-google", key: "g" + e.id });
        return;
      }
      var end = e.end && e.end > e.start ? e.end : ymd(addDays(dayStart(e.start), 1));
      allDay.push({ first: e.start, end: end, title: e.title, event: e, cls: "cal-google", key: "g" + e.id });
    });
    return { timed: timed, allDay: allDay };
  }
  function renderTimeline(body, mode) {
    var today = ymd(new Date());
    if (!calFocus) { calFocus = calDay || today; }
    var focus = dayStart(calFocus);
    var first = mode === "hafta" ? mondayOf(focus) : focus;
    var count = mode === "hafta" ? 7 : 1;
    var days = [];
    for (var i = 0; i < count; i++) { days.push(addDays(first, i)); }
    var step = function (n) { calFocus = ymd(addDays(focus, n * count)); tlPicked = null; renderPanel(true); };
    var last = days[days.length - 1];
    var title = mode === "hafta"
      ? first.toLocaleDateString("tr-TR", { day: "numeric", month: "long" }) + " – " +
        last.toLocaleDateString("tr-TR", { day: "numeric", month: "long", year: "numeric" })
      : first.toLocaleDateString("tr-TR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    var unit = mode === "hafta" ? "hafta" : "gün";
    body.appendChild(h("div", { class: "cal-head" }, [
      h("button", { type: "button", class: "btn quiet", text: "‹", title: "Önceki " + unit, "aria-label": "Önceki " + unit, onclick: function () { step(-1); } }),
      h("strong", { class: "cal-title", text: title, "aria-live": "polite" }),
      h("button", { type: "button", class: "btn quiet", text: "›", title: "Sonraki " + unit, "aria-label": "Sonraki " + unit, onclick: function () { step(1); } }),
      button("Bugün", function () { calFocus = today; tlPicked = null; tlScroll = null; renderPanel(true); }, "quiet")]));
    // the six-week payload of the month holding the first day's Monday covers the whole week
    var key = monthKey(mondayOf(first));
    var stale = !monthData || monthData.key !== key ||
      monthData.agenda !== JSON.stringify(data.agenda && data.agenda.branches);
    if (stale) { loadMonth(key); }
    if (!monthData || monthData.key !== key) { body.appendChild(note("Yükleniyor…")); return; }
    var d = monthData.data;
    if (!d) { body.appendChild(note("Takvim okunamadı: " + (monthData.error || ""))); return; }
    if (d.google && d.google.status !== "ok" && d.google.status !== "disabled") {
      body.appendChild(note("Google Takvim etkinlikleri okunamadı (" + d.google.status + ")."));
    }
    var entries = timelineEntries(d), picked = null;
    var grid = h("div", { class: "tl tl-" + mode, style: "--tl-cols: " + count });
    grid.appendChild(h("div", { class: "tl-corner" }));
    days.forEach(function (date) {
      var day = ymd(date);
      var label = mode === "hafta"
        ? date.toLocaleDateString("tr-TR", { weekday: "short", day: "numeric" })
        : date.toLocaleDateString("tr-TR", { weekday: "long" });
      grid.appendChild(mode === "hafta"
        ? h("button", { type: "button", class: "tl-dayhead" + (day === today ? " cal-today" : ""), text: label,
            title: "Bu günü saat saat aç", onclick: function () { openDay(day); } })
        : h("div", { class: "tl-dayhead" + (day === today ? " cal-today" : ""), text: label }));
    });
    grid.appendChild(h("div", { class: "tl-corner tl-alllabel", text: "tüm gün" }));
    days.forEach(function (date) {
      var day = ymd(date), cell = h("div", { class: "tl-allday" });
      entries.allDay.forEach(function (e) {
        if (!coversDay(e.first, e.end, day)) { return; }
        if (tlPicked === e.key) { picked = e; }
        cell.appendChild(h("button", { type: "button", class: "tl-chip " + e.cls, text: e.title, title: e.title,
          onclick: function () { tlPicked = e.key; renderPanel(true); } }));
      });
      grid.appendChild(cell);
    });
    var axis = h("div", { class: "tl-axis" });
    for (var hour = 0; hour < 24; hour++) {
      axis.appendChild(h("span", { class: "tl-hour", style: "top: " + hour * TL_HOUR + "px", text: pad2(hour) + ":00" }));
    }
    grid.appendChild(axis);
    var now = new Date();
    days.forEach(function (date) {
      var day = ymd(date), from = date.getTime(), to = addDays(date, 1).getTime();
      var col = h("div", { class: "tl-col" + (day === today ? " tl-today" : "") });
      var slices = [];
      entries.timed.forEach(function (e) {
        var s = daySlice(e.a, e.b, from, to);
        if (s) { s.entry = e; slices.push(s); }
      });
      layoutLanes(slices).forEach(function (s) {
        var e = s.entry;
        if (tlPicked === e.key + day) { picked = { entry: e, slice: s }; }
        var height = Math.max((s.bottom - s.top) / 60 * TL_HOUR, TL_MIN_BLOCK);
        var range = clockLabel(s.top) + (e.marker ? "" : "–" + clockLabel(s.bottom));
        var text = (s.before ? "(devam) " : "") + range + " " + e.title;
        col.appendChild(h("button", { type: "button", title: text,
          class: "tl-block " + e.cls + (s.before ? " tl-before" : "") + (s.after ? " tl-after" : "") + (e.marker ? " tl-marker" : ""),
          style: "top: " + s.top / 60 * TL_HOUR + "px; height: " + height + "px; left: calc(" + (s.lane * 100 / s.lanes) +
            "% + 1px); width: calc(" + (100 / s.lanes) + "% - 2px)",
          onclick: function () { tlPicked = e.key + day; renderPanel(true); } }, [
          h("span", { class: "tl-time", text: (s.before ? "(devam) " : "") + range }), " ", e.title]));
      });
      if (day === today) {
        var minutes = (now.getTime() - from) / 60000;
        col.appendChild(h("div", { class: "tl-now", style: "top: " + minutes / 60 * TL_HOUR + "px", title: "Şimdi " + hhmm(now) }));
      }
      grid.appendChild(col);
    });
    var wrap = h("div", { class: "tl-wrap", role: "region", "aria-label": title,
      onscroll: function () { tlScroll = { top: wrap.scrollTop, left: wrap.scrollLeft }; } }, [grid]);
    body.appendChild(wrap);
    // start at about 07:00; every hour stays reachable by scrolling
    setTimeout(function () {
      if (tlScroll) { wrap.scrollTop = tlScroll.top; wrap.scrollLeft = tlScroll.left; }
      else { wrap.scrollTop = 7 * TL_HOUR; }
    }, 0);
    if (picked && picked.slice) {
      var s = picked.slice, part = (s.before || s.after)
        ? { when: clockLabel(s.top) + "–" + clockLabel(s.bottom), cont: s.before } : null;
      body.appendChild(entryCard({ title: picked.entry.title, item: picked.entry.item, event: picked.entry.event,
        cls: picked.entry.cls, part: part }));
    } else if (picked) {
      body.appendChild(entryCard({ title: picked.title, item: picked.item, event: picked.event, cls: picked.cls }));
    } else {
      body.appendChild(note("Ayrıntı ve işlemler için bir kayda dokun."));
    }
  }

  function renderAgenda(body) {
    if (!me.user) { body.appendChild(note("Görev Takvimi hesap sistemi açıkken çalışır.")); return; }
    if (data.agenda && data.agenda.sources) { renderSources(body, data.agenda.sources); }
    body.appendChild(h("div", { class: "tabs", role: "group", "aria-label": "Görünüm" }, [["liste", "Liste"], ["takvim", "Takvim"], ["hafta", "Hafta"], ["gun", "Gün"]].map(function (v) {
      return h("button", { type: "button", class: "chip", "aria-pressed": v[0] === agendaView ? "true" : "false",
        text: v[1], onclick: function () { if (v[0] !== agendaView) { setAgendaView(v[0]); } } });
    })));
    if (data.agenda && data.agenda.sync) { body.appendChild(syncLine(data.agenda.sync)); }
    if (agendaView === "takvim") { renderMonth(body); agendaForm(body); return; }
    if (agendaView === "hafta" || agendaView === "gun") { renderTimeline(body, agendaView); agendaForm(body); return; }
    body.appendChild(h("div", { class: "tabs", role: "group", "aria-label": "Dal" }, BRANCHES.map(function (b) {
      return h("button", { type: "button", class: "chip", "aria-pressed": b[0] === agendaBranch ? "true" : "false",
        text: b[1], onclick: function () { agendaBranch = b[0]; renderPanel(true); } });
    })));
    var d = data.agenda;
    if (d === undefined) { body.appendChild(note("Yükleniyor…")); }
    else if (!d || !d.branches) { body.appendChild(note("Görev Takvimi okunamadı.")); }
    else {
      var sections = d.branches[agendaBranch] || {}, any = false;
      AGENDA_SECTIONS.forEach(function (sec) {
        var items = sections[sec[0]] || [];
        if (!items.length) { return; }
        any = true;
        body.appendChild(h("h3", { class: sec[0] === "overdue" ? "risk-high" : null, text: sec[1] }));
        items.forEach(function (it) { body.appendChild(agendaCard(it)); });
      });
      if (!any) { body.appendChild(note("Bu dalda açık görev yok.")); }
    }
    agendaForm(body);
  }

  var coreDocument = null, coreLoading = null;
  function loadCoreDocument(id) {
    coreLoading = id;
    return api("/api/core/doc?id=" + encodeURIComponent(id)).then(function (doc) {
      if (coreLoading !== id) { return; }
      coreDocument = doc; coreLoading = null; renderPanel(true);
    }, function () {
      if (coreLoading !== id) { return; }
      coreDocument = null; coreLoading = null;
      if (openKey === "core") { renderPanel(true); $("panel-body").appendChild(note("Belge açılamadı. Yeniden dene.")); }
    });
  }
  function renderCore(body) {
    if (!data.core) { body.appendChild(note("Core kullanılamıyor.")); return; }
    var status = h("p", { role: "status", class: "muted" });
    var select = h("select", { "aria-label": "Core'a eklenecek dosya" },
      [h("option", { value: "", text: "Yüklenmiş dosya seç" })].concat(list("files", "files").map(function (file) {
        return h("option", { value: file.id, text: file.name });
      })));
    var add = button("Core'a ekle", function () {
      if (!select.value) { status.textContent = "Önce bir dosya seç."; return; }
      add.disabled = true;
      api("/api/core/add", { file_id: select.value }).then(function (doc) {
        coreDocument = doc; status.textContent = "Belge okunuyor…";
        return refresh("core");
      }, function () { status.textContent = "Belge eklenemedi. Yeniden dene."; }).then(function () { add.disabled = false; });
    });
    body.appendChild(h("div", { class: "row agenda-add" }, [select, add,
      button("Dosya yükle", function () { open("files"); })]));
    body.appendChild(status);
    var docs = list("core", "documents");
    if (!docs.length) { body.appendChild(note("Henüz ders belgesi yok. Dosyalar'dan yükleyip Core'a ekle.")); }
    docs.forEach(function (doc) {
      var mark = doc.read_as === "reading" ? "Okunuyor…" : doc.read_as === "failed" ? (doc.error || "Okunamadı")
        : /^(image|mixed)$/.test(doc.read_as) ? "Görselden okundu · hata içerebilir" : "Metinden okundu";
      var remove = button("Sil", function () {
        remove.disabled = true;
        fetch("/api/core/doc?id=" + encodeURIComponent(doc.id), { method: "DELETE", credentials: "same-origin",
          headers: { "X-CSRF-Token": csrf } }).then(function (response) {
          if (!response.ok) { throw new Error(); }
          if (coreDocument && coreDocument.id === doc.id) { coreDocument = null; }
          coreLoading = null;
          return refresh("core");
        }).catch(function () { status.textContent = "Belge silinemedi. Yeniden dene."; remove.disabled = false; });
      }, "quiet");
      body.appendChild(card([button(doc.title, function () { loadCoreDocument(doc.id); }, "quiet"), note(mark), remove]));
    });
    if (coreLoading) { body.appendChild(note("Belge açılıyor…")); }
    if (!coreDocument) { return; }
    var current = docs.filter(function (doc) { return doc.id === coreDocument.id; })[0];
    if (!current) { coreDocument = null; return; }
    if (coreDocument.read_as === "reading" && current.read_as !== "reading" && !coreLoading) {
      loadCoreDocument(current.id); return;
    }
    body.appendChild(h("h3", { text: coreDocument.title }));
    body.appendChild(h("p", { class: "muted", text: coreDocument.card }));
    (coreDocument.pages || []).forEach(function (page) {
      var editor = h("textarea", { rows: "7", "aria-label": "Sayfa " + page.n,
        oninput: function () { page.text = editor.value; } });
      editor.value = page.text;
      var saved = h("p", { role: "status", class: "muted" });
      var save = button("Sayfayı kaydet", function () {
        save.disabled = true;
        api("/api/core/page", { id: coreDocument.id, n: page.n, text: editor.value }).then(function () {
          saved.textContent = "Kaydedildi."; return refresh("core");
        }, function () { saved.textContent = "Kaydedilemedi. Yeniden dene."; }).then(function () { save.disabled = false; });
      });
      body.appendChild(card([h("strong", { text: "Sayfa " + page.n }),
        page.from_image ? note("Görselden okundu · formülleri kontrol edip düzelt.") : null, editor, save, saved]));
    });
  }

  var PANELS = {
    core: { title: "Core", sub: "Ders belgelerin · kartlar ve düzenlenebilir sayfalar", render: renderCore },
    waiting: { title: "Bekleyen kararlar", sub: "Onaylar ve yanıt bekleyen görevler", render: function (body) {
      var items = list("approvals", "approvals");
      var waits = tasksBy(["waiting_for_user"]);
      if (!me.user) { body.appendChild(note("Onaylar hesap sistemi açıkken çalışır.")); return; }
      if (!items.length && !waits.length) { body.appendChild(note("Şu an senden karar bekleyen bir şey yok.")); }
      items.forEach(function (a) {
        var row = h("div", { class: "row" });
        var answer = function (ok) {
          return function () {
            [].forEach.call(row.querySelectorAll("button"), function (b) { b.disabled = true; });
            api("/api/approvals/answer", { id: a.request_id, approved: ok }).then(function () { return refresh("approvals"); },
              function (err) { row.appendChild(note("Gönderilemedi: " + err.message)); });
          };
        };
        row.appendChild(button("Onayla", answer(true)));
        row.appendChild(button("Reddet", answer(false), "danger"));
        body.appendChild(card([h("strong", { class: "approval-summary", text: a.summary || a.action }), note(a.action), row]));
      });
      waits.forEach(function (t) { body.appendChild(taskCard(t)); });
    } },
    mail: { title: "Mail", sub: "Gelen kutusu · yalnızca okuma", render: function (body) {
      var d = data.inbox;
      if (!d) { body.appendChild(note(d === null ? "Gelen kutusu izleme kapalı." : "Yükleniyor…")); return; }
      var st = d.status || {};
      var line = { starting: "İlk kontrol bekleniyor.", disabled: "Gelen kutusu izleme bu sunucuda kapalı.",
        not_configured: "Gmail bağlı değil. Terminalde: python tools/gmail_auth.py login",
        auth_down: "Gmail bağlantısı düştü. Terminalde yeniden giriş yap: python tools/gmail_auth.py login" }[st.state];
      if (st.state === "ok") { line = "Son kontrol " + isoClock(st.last_poll); }
      if (st.state === "error") { line = "Son kontrol başarısız (" + isoClock(st.last_poll) + "): " + (st.error || ""); }
      body.appendChild(note(line || ""));
      var problems = inboxProblems();
      if (problems.length) { body.appendChild(note("Dikkat: " + problems.join(" · "))); }
      if (!d.mails.length) { body.appendChild(note("Henüz kayıtlı mail yok.")); return; }
      // filter chips: every category with its count; a phone message comes only for Görev and Güvenlik
      var filters = [["hepsi", "Hepsi", d.mails.length]].concat(MAIL_CATS.map(function (c) {
        return [c[0], c[1], mailsIn(c[0]).length];
      }));
      body.appendChild(h("div", { class: "tabs", role: "group", "aria-label": "Mail kategorisi" }, filters.map(function (f) {
        var label = f[1] + " " + f[2];
        return h("button", { type: "button", class: "chip", "aria-pressed": f[0] === mailFilter ? "true" : "false",
          text: label, title: label, onclick: function () { mailFilter = f[0]; renderPanel(true); } });
      })));
      var shown = d.mails.filter(function (m) { return mailFilter === "hepsi" || mailCategory(m) === mailFilter; });
      if (!shown.length) { body.appendChild(note("Bu kategoride mail yok.")); }
      shown.forEach(function (m) {
        var cat = mailCategory(m);
        var catName = (MAIL_CATS.filter(function (c) { return c[0] === cat; })[0] || [cat, cat])[1];
        var tags = [h("span", { class: "tag " + (MAIL_CAT_CLASS[cat] || ""), text: catName })];
        if (m.origin === "okul") { tags.push(h("span", { class: "tag", text: "Okul", title: "Okul mailinden yönlendirildi" })); }
        tags.push(h("span", { class: "tag risk-" + m.risk, text: RISKS[m.risk] || m.risk }));
        tags.push(h("span", { class: "muted small", text: isoClock(m.seen_at) }));
        var kids = [h("strong", { text: m.subject || "(konu yok)" }), note(m.sender || ""), h("p", {}, tags)];
        if (m.reason) { kids.push(h("p", { class: "small", text: m.reason })); }
        if (m.task && m.task.title) {
          var when = m.task.due || m.task.start;
          kids.push(h("p", { class: "small", text: "Yapılacak: " + m.task.title + (when ? " · " + when.slice(0, 16).replace("T", " ") : "") }));
        }
        body.appendChild(card(kids));
      });
    } },
    tasks: { title: "Görevler", sub: "Arka planda çalışan işler", render: function (body) {
      if (!me.user) { body.appendChild(note("Görevler hesap sistemi açıkken çalışır.")); return; }
      var input = h("textarea", { rows: "2", placeholder: "Igris arka planda ne yapsın?", "aria-label": "Yeni görev" });
      var status = note("");
      var submit = button("Kuyruğa ekle", function () {
        var prompt = input.value.trim();
        if (!prompt) { input.focus(); return; }
        submit.disabled = true;
        api("/api/tasks/submit", { prompt: prompt }).then(function () {
          input.value = "";
          status.textContent = "Görev kuyruğa eklendi.";
          return refresh("tasks");
        }, function () { status.textContent = "Görev eklenemedi. Kuyruk sınırını ve bağlantını kontrol et."; })
          .then(function () { submit.disabled = false; });
      });
      body.appendChild(card([input, h("div", { class: "row" }, [submit]), status]));
      var d = data.tasks;
      if (d) { body.appendChild(note("Hesabında en fazla " + d.max_pending + " bekleyen görev · sunucuda aynı anda " + d.max_workers)); }
      var items = list("tasks", "tasks");
      if (!items.length) { body.appendChild(note("Henüz görev yok.")); }
      items.forEach(function (t) { body.appendChild(taskCard(t)); });
    } },
    files: { title: "Dosyalar", sub: "Yüklediğin belgeler", render: function (body) {
      if (!me.user) { body.appendChild(note("Dosyalar hesap sistemi açıkken çalışır.")); return; }
      var d = data.files;
      if (!d) { body.appendChild(note("Yükleniyor…")); return; }
      var picker = h("input", { type: "file", multiple: true, "aria-label": "Yüklenecek dosyalar" });
      var status = note(fileSize(d.used_bytes) + " / " + fileSize(d.max_total_bytes) + " · dosya başına en fazla "
        + fileSize(d.max_file_bytes) + " · " + d.files.length + " / " + d.max_count + " dosya");
      var up = button("Yükle", function () { upload(picker, status, up); });
      body.appendChild(card([picker, h("div", { class: "row" }, [up]), status]));
      if (!d.files.length) { body.appendChild(note("Henüz dosya yüklemedin.")); }
      d.files.forEach(function (f) {
        var del = button("Sil", function () {
          if (!window.confirm(f.name + " dosyası kalıcı olarak silinsin mi?")) { return; }
          del.disabled = true;
          api("/api/files/delete", { id: f.id }).then(function () { return refresh("files"); },
            function () { del.disabled = false; status.textContent = "Dosya silinemedi."; });
        }, "danger");
        body.appendChild(card([h("strong", { text: f.name }), note(fileSize(f.size)), h("div", { class: "row" }, [
          h("a", { class: "btn quiet", href: "/api/files/" + encodeURIComponent(f.id) + "/download", text: "İndir" }),
          button("Oracle'da kullan", function () {
            oracle.open("Yüklediğim " + f.name + " dosyasını incele. Dosya kimliği: " + f.id);
          }, "quiet"), del])]));
      });
    } },
    multiagent: { title: "Multiagent", sub: "Ajan ekibinin işleri", render: function (body) {
      var d = data.jobs;
      if (!d) { body.appendChild(note(d === null ? "Multiagent bu sunucuda kapalı." : "Yükleniyor…")); return; }
      body.appendChild(note("Aynı anda en fazla " + d.max_parallel + " iş."));
      var items = list("jobs", "jobs").slice().reverse();
      if (!items.length) { body.appendChild(note("Henüz iş yok.")); }
      items.slice(0, 20).forEach(function (j) {
        var row = h("div", { class: "row" });
        if ((j.status === "queued" || j.status === "running") && j.owned !== false) {
          var stop = button("İptal et", function () {
            if (!window.confirm("Bu multiagent işi durdurulsun mu?")) { return; }
            stop.disabled = true;
            api("/api/multiagent/cancel", { id: j.id }).then(function () { return refresh("jobs"); },
              function (err) { stop.disabled = false; row.appendChild(note("Durdurulamadı: " + err.message)); });
          }, "danger");
          row.appendChild(stop);
        }
        var when = j.started ? hhmm(new Date(j.started * 1000)) : "";
        body.appendChild(card([h("strong", { text: j.goal }),
          note((JOB_STATES[j.status] || j.status) + (j.implement ? " · kod yazıyor" : " · rapor") + (when ? " · " + when : "")
            + (j.run_id ? " · " + j.run_id : "")), row]));
      });
    } },
    models: { title: "Modeller", sub: "Sağlayıcılar ve günlük kullanım", render: function (body) {
      var p = list("models", "providers");
      if (!data.models) { body.appendChild(note("Yükleniyor…")); return; }
      var ma = data.models.multiagent;
      if (ma) {
        body.appendChild(h("h3", { text: "Multiagent işçileri" }));
        if (ma.error) {
          body.appendChild(note("Multiagent işçi listesi okunamadı: " + ma.error));
        } else {
          body.appendChild(note("Yönetici: " + ma.yonetici.model + " (" + ma.yonetici.backend + ")"
            + " · Astra: " + ma.astra.model + " (" + ma.astra.backend + ")"
            + (ma.rotasyon ? " · bulut işçileri sırayla döner" : " · sabit zincir sırası")));
          var off = [];
          ma.isciler.forEach(function (w) {
            if (w.durum !== "aktif") { off.push(w); return; }
            var bits = [w.tur || "", w.rutbe ? "rütbe " + w.rutbe : "rütbesiz"];
            if (w.paralel) { bits.push(w.paralel + " paralel"); }
            if (w.rpm) { bits.push(w.rpm + "/dk"); }
            if (w.rpd) { bits.push(w.rpd + "/gün"); }
            if (w.gecerlilik_bitis) { bits.push("bitiş " + String(w.gecerlilik_bitis).slice(0, 10)); }
            body.appendChild(card([h("strong", { text: (w.sira ? w.sira + ". " : "") + w.ad }),
              note(w.model), note(bits.filter(Boolean).join(" · "))]));
          });
          if (!ma.isciler.some(function (w) { return w.durum === "aktif"; })) {
            body.appendChild(note("Aktif işçi yok."));
          }
          var REASONS = { anahtar_yok: "anahtar yok", zincir_disi: "zincirde değil",
            suresi_doldu: "süresi doldu", pasif: "pasif", kayit_hatali: "kayıt hatalı" };
          if (off.length) {
            var more = h("details", {}, [h("summary", { text: off.length + " kayıt kullanılmıyor" })]);
            off.forEach(function (w) {
              more.appendChild(note(w.ad + " (" + w.model + ") · " + (REASONS[w.durum] || w.durum)
                + (w.neden ? ": " + w.neden : "")));
            });
            body.appendChild(more);
          }
          (ma.hatalar || []).forEach(function (e) {
            body.appendChild(h("p", { class: "small risk-medium", text: "Okunamayan kayıt: " + e }));
          });
        }
        body.appendChild(h("h3", { text: "Igris sohbet modelleri" }));
      }
      p.forEach(function (m) {
        var kids = [h("strong", { text: m.name }), note((m.models || []).join(", "))];
        if (m.health) { kids.push(note("Durum: " + m.health)); }
        if (m.trains_on_data) { kids.push(h("p", { class: "small risk-medium", text: "Bu sağlayıcı verini eğitimde kullanabilir." })); }
        if (Number.isFinite(m.daily_limit) && Number.isFinite(m.requests_used_today)) {
          var used = Math.min(m.daily_limit, Math.max(0, m.requests_used_today));
          kids.push(note(used + " / " + m.daily_limit + " istek bugün"));
          kids.push(h("progress", { max: m.daily_limit, value: used }));
        } else { kids.push(note("Kota bilinmiyor")); }
        body.appendChild(card(kids));
      });
    } },
    roadmap: { title: "Yol haritası", sub: "Yenilikler ve planlar", render: function (body) {
      var d = data.roadmap;
      if (!d) { body.appendChild(note("Yükleniyor…")); return; }
      body.appendChild(h("h3", { text: "Planlar" }));
      (d.plans || []).forEach(function (p) { body.appendChild(roadCard(p)); });
      body.appendChild(h("h3", { text: "Yayınlananlar" }));
      (d.releases || []).forEach(function (p) { body.appendChild(roadCard(p)); });
    } },
    calendar: { title: "Görev Takvimi", sub: "Sınavlar, ödevler, toplantılar · Google Takvim", render: function (body) {
      renderAgenda(body);
      if (!allowed("admin") || agendaView === "takvim") { return; }  // the grid shows Google events
      body.appendChild(h("h3", { text: "Google Takvim · önümüzdeki 14 gün" }));
      var d = data.calendar;
      if (d === undefined) { body.appendChild(note("Yükleniyor…")); return; }
      var status = d ? d.status : "disabled";
      var help = {
        disabled: "Takvim bu sunucuda kapalı.",
        not_configured: "Google'a giriş yapılmamış. Terminalde: python tools/gmail_auth.py login",
        needs_consent: "Google girişin takvim iznini içermiyor. Terminalde bir kez: python tools/gmail_auth.py login (Google Cloud projesinde Google Calendar API açık olmalı).",
        auth_down: "Google bağlantısı düştü. Terminalde yeniden giriş yap: python tools/gmail_auth.py login",
        error: "Takvim okunamadı" + (d && d.error ? ": " + d.error : ".")
      }[status];
      if (status !== "ok") { body.appendChild(note(help || "Takvim okunamadı.")); return; }
      var events = calEvents();
      if (!events.length) { body.appendChild(note("Önümüzdeki 14 günde etkinlik yok.")); }
      var lastDay = "";
      events.forEach(function (e) {
        var day = eventStart(e).toLocaleDateString("tr-TR", { weekday: "long", day: "numeric", month: "long" });
        if (day !== lastDay) { body.appendChild(h("h3", { text: day })); lastDay = day; }
        body.appendChild(card([h("strong", { text: e.title }),
          note((e.all_day ? "tüm gün" : hhmm(eventStart(e)) + " – " + hhmm(new Date(e.end))) + (e.location ? " · " + e.location : ""))]));
      });
      body.appendChild(button("Oracle'a hatırlatma yazdır", function () { oracle.open("Bana bir hatırlatma görevi oluştur: "); }, "quiet"));
    } },
    account: { title: "Hesap", sub: "Oturum ve görünüm", render: function (body) {
      if (!me.user) { body.appendChild(note("Yerel kullanım: hesap sistemi kapalı.")); }
      else {
        var out = button("Çıkış yap", function () {
          out.disabled = true;
          api("/api/auth/logout", {}).then(function () { window.location.replace("/login"); },
            function () { out.disabled = false; });
        }, "danger");
        body.appendChild(card([h("strong", { text: me.user.display_name }),
          note(me.user.role === "admin" ? "Yönetici" : "Kullanıcı"),
          h("div", { class: "row" }, [out])]));
      }
      var st = data.state;
      if (st) { body.appendChild(conversationCard(st)); }
      if (me.user) { body.appendChild(passwordCard()); }
      if (st) {
        body.appendChild(card([h("strong", { text: (st.name || "Igris") + " v" + st.version }),
          note("Oturum: " + st.session + " · kişilik: " + st.persona),
          note("Model sırası: " + (st.backends || []).join(" → ")),
          note((st.tools || []).length + " araç hazır")]));
      }
      body.appendChild(card([h("strong", { text: "Hareket" }),
        note("Efektleri kapatınca çark durur, göz kırpmaz ve sahne tek kare çizilir."),
        h("div", { class: "row" }, [button(motion ? "Hareketi kapat" : "Hareketi aç", function () { setMotion(!motion); render(); }, "quiet")])]));
    } }
  };

  // ---------- Hesap: conversation session, persona and password ----------
  function choice(id, label, names, active) {
    return h("select", { id: id, "aria-label": label }, (names || []).map(function (n) {
      return h("option", { value: n, selected: n === active, text: n });
    }));
  }
  // POST /api/session or /api/persona; the answer is the new /api/state
  function switchTo(path, name, status, restore) {
    status.textContent = "";
    return api(path, { name: name }).then(function (st) {
      data.state = st;
      oracle.reload();
      renderPanel(true);
    }, function (err) {
      status.textContent = err.message;
      if (restore) { restore(); }
    });
  }
  function conversationCard(st) {
    var status = h("p", { class: "small risk-high", role: "alert" });
    var session = choice("v2-session", "Sohbet oturumu", st.sessions, st.session);
    session.addEventListener("change", function () {
      switchTo("/api/session", session.value, status, function () { session.value = st.session; });
    });
    var fresh = h("input", { type: "text", id: "v2-session-new", maxlength: "64", placeholder: "Yeni oturum adı",
      "aria-label": "Yeni oturum adı" });
    var create = button("Yeni oturum", function () {
      var name = fresh.value.trim();
      if (!name) { fresh.focus(); return; }
      create.disabled = true;
      switchTo("/api/session", name, status).then(function () { create.disabled = false; });
    }, "quiet");
    fresh.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); create.click(); }
    });
    var persona = choice("v2-persona", "Kişilik", st.personas, st.persona);
    persona.addEventListener("change", function () {
      switchTo("/api/persona", persona.value, status, function () { persona.value = st.persona; });
    });
    return card([h("strong", { text: "Sohbet oturumu" }),
      h("div", { class: "account-form" }, [
        h("label", { class: "small muted", for: "v2-session", text: "Etkin oturum" }), session,
        h("div", { class: "account-line" }, [fresh, create]),
        h("label", { class: "small muted", for: "v2-persona", text: "Kişilik" }), persona]),
      status]);
  }
  function passwordCard() {
    var errors = {
      "Invalid current password": "Mevcut parola yanlış.",
      "Password must contain 12 to 256 characters": "Yeni parola 12 ile 256 karakter arasında olmalı."
    };
    var field = function (id, label, auto) {
      return [h("label", { class: "small muted", for: id, text: label }),
        h("input", { type: "password", id: id, autocomplete: auto, maxlength: "256", required: true })];
    };
    var status = h("p", { class: "small risk-high", role: "alert" });
    var submit = h("button", { type: "submit", class: "btn", text: "Parolayı değiştir", title: "Parolayı değiştir" });
    var form = h("form", { class: "account-form", "aria-label": "Parola değiştir" },
      field("v2-pass-current", "Mevcut parola", "current-password")
        .concat(field("v2-pass-new", "Yeni parola", "new-password"),
          field("v2-pass-repeat", "Yeni parola (tekrar)", "new-password"),
          [h("div", { class: "row" }, [submit])]));
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (submit.disabled) { return; }
      var password = $("v2-pass-new").value;
      if (password !== $("v2-pass-repeat").value) { status.textContent = "Yeni parolalar aynı değil"; return; }
      status.textContent = "";
      submit.disabled = true;
      api("/api/auth/password", { current_password: $("v2-pass-current").value, password: password }).then(function () {
        // the server signed every browser out, this one too
        window.location.replace("/login");
      }, function (err) {
        status.textContent = errors[err.message] || "Parola değiştirilemedi. Lütfen tekrar dene.";
        submit.disabled = false;
      });
    });
    return card([h("strong", { text: "Parola değiştir" }), form, status]);
  }

  function taskCard(t) {
    var result = h("pre", { class: "result", hidden: !taskResults[t.id], text: taskResults[t.id] || "" });
    var row = h("div", { class: "row" });
    if (t.status === "waiting_for_user") {
      row.appendChild(button("Yanıtla", function () {
        var answer = window.prompt(t.waiting_question || "Yanıtın");
        if (!answer || !answer.trim()) { return; }
        api("/api/tasks/resume", { id: t.id, answer: answer }).then(function () { return refresh("tasks"); },
          function () { row.appendChild(note("Yanıt gönderilemedi.")); });
      }));
    }
    if (["queued", "running", "waiting_for_user", "waiting_for_event"].indexOf(t.status) >= 0) {
      row.appendChild(button("İptal et", function () {
        api("/api/tasks/cancel", { id: t.id }).then(function () { return refresh("tasks"); },
          function () { row.appendChild(note("Görev iptal edilemedi.")); });
      }, "danger"));
    }
    if (["done", "failed", "interrupted"].indexOf(t.status) >= 0) {
      row.appendChild(button("Sonucu göster", function () {
        api("/api/tasks/" + encodeURIComponent(t.id)).then(function (d) {
          taskResults[t.id] = d.result || "Görev tamamlanamadı. Model bağlantısını kontrol edip yeniden dene.";
          result.textContent = taskResults[t.id];
          result.hidden = false;
        }, function () { row.appendChild(note("Görev sonucu alınamadı.")); });
      }, "quiet"));
    }
    return card([h("strong", { text: String(t.prompt || "").slice(0, 160) }),
      note((TASK_STATES[t.status] || t.status) + (t.waiting_question ? " · " + t.waiting_question : "")), row, result]);
  }

  function roadCard(p) {
    var kids = [h("strong", { text: p.title }), note((ROAD[p.status] || p.status || "") + (p.date ? " · " + p.date : ""))];
    if (Array.isArray(p.items) && p.items.length) {
      kids.push(h("ul", { class: "small" }, p.items.slice(0, 6).map(function (i) { return h("li", { text: i }); })));
    }
    return card(kids);
  }

  function upload(picker, status, btn) {
    var files = [].slice.call(picker.files || []);
    var d = data.files;
    if (!files.length || !d) { return; }
    btn.disabled = true;
    var failed = [], done = 0;
    files.reduce(function (chain, f, i) {
      return chain.then(function () {
        status.textContent = "Yükleniyor (" + (i + 1) + "/" + files.length + "): " + f.name;
        if (f.size > d.max_file_bytes) { return Promise.reject(new Error("boyut sınırı")); }
        return new Promise(function (resolve, reject) {
          var reader = new FileReader();
          reader.onerror = function () { reject(new Error("okunamadı")); };
          reader.onload = function () { resolve(String(reader.result).split(",")[1] || ""); };
          reader.readAsDataURL(f);
        }).then(function (encoded) { return api("/api/files/upload", { name: f.name, content: encoded }); });
      }).then(function () { done += 1; }, function (err) {
        failed.push(f.name + " (" + (err.status === 413 ? "dosya veya depolama sınırı" : err.message) + ")");
      });
    }, Promise.resolve()).then(function () {
      status.textContent = done + "/" + files.length + " dosya yüklendi." + (failed.length ? " Yüklenemeyenler: " + failed.join(", ") : "");
      btn.disabled = false;
      return refresh("files");
    });
  }

  function open(key) {
    if (key === "oracle") { oracle.open(); return; }
    var m = MODULES.filter(function (x) { return x.key === key; })[0];
    if (m && m.need && !allowed(m.need)) {
      openKey = null;
      showPanel(m.name, offText(m), function (body) {
        body.appendChild(note(m.need === "account" ? "Bu modül hesap sistemi açıkken çalışır (python -m igris --setup-users)."
          : "Bu modül yalnızca yönetici hesabıyla açılır."));
      });
      return;
    }
    if (key === "study") { window.location.assign("/calis"); return; }
    openKey = key;
    renderPanel(true);
    $("panel-close").focus();
  }
  function showPanel(title, sub, fill) {
    $("panel-title").textContent = title;
    $("panel-sub").textContent = sub;
    var body = $("panel-body");
    var scroll = body.scrollTop;
    clear(body);
    fill(body);
    body.scrollTop = scroll;
    $("panel").hidden = false;
  }
  function closePanel() { openKey = null; $("panel").hidden = true; }
  // A panel is rebuilt only when the data it shows changed, and never while the owner types in it.
  var PANEL_KEYS = { core: ["core", "files"], waiting: ["approvals", "tasks"], mail: ["inbox"], tasks: ["tasks"], files: ["files"],
    multiagent: ["jobs"], calendar: ["calendar", "agenda"], models: ["models"], roadmap: ["roadmap"], account: ["state"] };
  var panelSig = "";
  function renderPanel(force) {
    if (!openKey || !PANELS[openKey]) { return; }
    var p = PANELS[openKey];
    var sig = openKey + JSON.stringify((PANEL_KEYS[openKey] || []).map(function (k) { return data[k]; })) + motion;
    if (!force && sig === panelSig) { return; }
    var active = document.activeElement;
    if (!force && active && $("panel").contains(active) && /^(TEXTAREA|INPUT|SELECT)$/.test(active.tagName)) { return; }
    // a half-typed password would be lost on a rebuild
    if (!force && [].some.call($("panel").querySelectorAll("input[type=password]"), function (i) { return i.value; })) { return; }
    panelSig = sig;
    showPanel(p.title, p.sub, p.render);
  }

  // ---------- views: Odak (the wheel) and Klasik (module tabs, the panel full width) ----------
  // Klasik reuses MODULES and PANELS: a tab opens the same panel, which CSS lays out as the page.
  var VIEW_KEY = "igris.v2.view";
  var VIEWS = ["odak", "klasik"];
  var view = "odak";
  var klasikKey = null;
  var tabs = {};
  function readLocalView() {
    try { var v = localStorage.getItem(VIEW_KEY); if (VIEWS.indexOf(v) >= 0) { return v; } } catch (e) { /* storage blocked */ }
    return "odak";
  }
  function fitTop() {
    document.documentElement.style.setProperty("--top-h", document.querySelector(".top").offsetHeight + "px");
  }
  function firstTab() {
    var m = MODULES.filter(function (x) { return x.key !== "oracle" && !x.navigates && (!x.need || allowed(x.need)); })[0];
    return m ? m.key : "waiting";
  }
  function applyView(v) {
    view = VIEWS.indexOf(v) >= 0 ? v : "odak";
    document.documentElement.setAttribute("data-view", view);
    VIEWS.forEach(function (k) { $("view-" + k).setAttribute("aria-pressed", k === view ? "true" : "false"); });
    $("klasik-tabs").hidden = view !== "klasik";
    hideTip();
    fitTop();
    if (view === "klasik") { selectTab(klasikKey || openKey || firstTab()); renderTabs(); }
    else { closePanel(); }
  }
  function chooseView(v) {
    applyView(v);
    if (me.user) { api("/api/prefs", { view: view }).catch(function () { /* kept for this visit only */ }); }
    else { try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* storage blocked */ } }
  }
  function tabItems() {
    return [{ key: "waiting", name: "Kararlar", count: waitingCount, info: function () { return PANELS.waiting.sub; } }].concat(MODULES);
  }
  function buildTabs() {
    var nav = $("klasik-tabs");
    tabItems().forEach(function (m) {
      var count = h("span", { class: "tab-count" });
      var b = h("button", { type: "button", role: "tab", class: "tab", id: "tab-" + m.key, "aria-selected": "false",
        tabindex: "-1", title: m.name, onclick: function () { selectTab(m.key); } },
        [h("span", { class: "tab-label", text: m.name }), count]);
      tabs[m.key] = { b: b, count: count, m: m };
      nav.appendChild(b);
    });
    nav.addEventListener("keydown", function (e) {
      var keys = Object.keys(tabs), i = keys.indexOf(document.activeElement && document.activeElement.id.slice(4));
      var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (i < 0 || !step) { return; }
      e.preventDefault();
      tabs[keys[(i + step + keys.length) % keys.length]].b.focus();
    });
  }
  function selectTab(key) {
    if (key === "oracle") { oracle.open(); return; }
    klasikKey = key;
    Object.keys(tabs).forEach(function (k) {
      tabs[k].b.setAttribute("aria-selected", k === key ? "true" : "false");
      tabs[k].b.setAttribute("tabindex", k === key ? "0" : "-1");
    });
    open(key);
    if (tabs[key] && tabs[key].b.scrollIntoView) { tabs[key].b.scrollIntoView({ block: "nearest", inline: "nearest" }); }
  }
  function renderTabs() {
    if (view !== "klasik") { return; }
    Object.keys(tabs).forEach(function (k) {
      var t = tabs[k], m = t.m, off = m.need && !allowed(m.need);
      var c = m.count && !off ? m.count() : 0;
      t.count.textContent = c ? String(c) : "";
      t.b.classList.toggle("off", !!off);
      t.b.title = m.name + ": " + (off ? offText(m) : m.info ? m.info() : "");
    });
  }

  // ---------- Oracle ----------
  var oracle = (function () {
    var loaded = false;
    var logEl = $("oracle-log");
    function bubble(role, text, meta) {
      var el = h("div", { class: "msg " + (role === "user" ? "user" : "assistant"), text: text });
      if (meta) { el.appendChild(h("span", { class: "meta", text: meta })); }
      logEl.appendChild(el);
      logEl.scrollTop = logEl.scrollHeight;
      return el;
    }
    var ready = Promise.resolve();
    function load() {
      if (loaded) { return; }
      loaded = true;
      ready = api("/api/history").then(function (d) {
        clear(logEl);
        var msgs = (d.messages || []).filter(function (m) { return m.role === "user" || m.role === "assistant"; });
        if (!msgs.length) { bubble("assistant", "Buradayım. Ne yapalım?"); }
        msgs.forEach(function (m) { bubble(m.role, m.content); });
      }, function (err) { loaded = false; bubble("assistant", "Geçmiş alınamadı: " + err.message); });
    }
    function send() {
      var input = $("oracle-input");
      var text = input.value.trim();
      if (!text || asking) { return; }
      input.value = "";
      asking = true;
      $("oracle-send").disabled = true;
      changed();
      var wait = null;
      ready.then(function () {
        bubble("user", text);
        wait = bubble("assistant", "…");
        return api("/api/ask", { text: text });
      }).then(function (d) {
        wait.textContent = d.text || "";
        var meta = (d.backends || []).join(", ");
        if (d.job_id) { meta = "multiagent'a devredildi · iş " + d.job_id; refresh("jobs"); }
        if (meta) { wait.appendChild(h("span", { class: "meta", text: meta })); }
      }, function (err) {
        if (!wait) { wait = bubble("assistant", ""); }
        wait.textContent = "Cevap alınamadı: " + err.message;
      }).then(function () {
        asking = false;
        $("oracle-send").disabled = false;
        changed();
        input.focus();
      });
    }
    $("oracle-form").addEventListener("submit", function (e) { e.preventDefault(); send(); });
    $("oracle-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    $("oracle-close").addEventListener("click", function () { close(); });
    function close() { $("oracle").hidden = true; }
    return {
      open: function (prefill) {
        $("oracle").hidden = false;
        load();
        var input = $("oracle-input");
        if (typeof prefill === "string") { input.value = prefill; }
        input.focus();
      },
      close: close,
      // another conversation was selected: load its history the way start-up does
      reload: function () { loaded = false; load(); },
      toggle: function () { if ($("oracle").hidden) { this.open(); } else { close(); } }
    };
  })();

  // ---------- render loop ----------
  var lastMood = "";
  function render() {
    var m = mood();
    if (m.key !== lastMood) {
      lastMood = m.key;
      eye.set(m.key);
      scene.set(MOODS[m.key]);
    }
    $("state-text").textContent = m.text;
    renderWheel();
    renderTabs();
    renderPanel(false);
    if (liveLines().join("|") !== lastLive) { buildScribbles(); }
  }
  listeners.push(render);

  // ---------- live change stream (server-sent events) ----------
  function connectEvents() {
    if (!window.EventSource) { return; }
    var es = new EventSource("/api/events");
    es.addEventListener("hello", function () { sseLive = true; refreshFailed(); });
    es.addEventListener("change", function (e) {
      var msg = {};
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      (msg.keys || []).forEach(function (k) { refresh(k); });
    });
    es.addEventListener("error", function () {
      sseLive = false;
      // 401/404 close the stream for good; polling carries on alone
      if (es.readyState === 2) { setTimeout(connectEvents, 60000); }
    });
  }

  function tick() { $("clock").textContent = hhmm(new Date()); }

  // ---------- wiring ----------
  $("panel-close").addEventListener("click", closePanel);
  $("oracle-hint").addEventListener("click", function () { oracle.toggle(); });
  $("motion-toggle").addEventListener("click", function () { setMotion(!motion); });
  VIEWS.forEach(function (k) { $("view-" + k).addEventListener("click", function () { if (k !== view) { chooseView(k); } }); });
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey && (e.code === "Space" || e.key === " ")) { e.preventDefault(); oracle.toggle(); return; }
    if (e.key === "Escape") {
      if (!$("oracle").hidden) { oracle.close(); }
      else if (!$("panel").hidden && view !== "klasik") { closePanel(); }
    }
  });
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { fitTop(); buildScribbles(); }, 200);
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { scene.kick(); } });
  window.addEventListener("pageshow", function (e) { if (e.persisted) { window.location.reload(); } });

  // ---------- boot ----------
  buildWheel();
  buildTabs();
  fitTop();
  setMotion(motion);
  buildScribbles();
  tick();
  setInterval(tick, 20000);
  render();
  api("/api/auth/me").then(function (d) {
    me = d || {};
    csrf = me.csrf || "";
  }, function () { me = { enabled: false, user: null }; }).then(function () {
    // the view is per user on the server; without accounts it lives in this browser only
    var chosen = me.user ? api("/api/prefs").then(function (d) { return d.view; }, function () { return "odak"; })
      : Promise.resolve(readLocalView());
    chosen.then(function (v) { if (v !== view) { applyView(v); } });
    POLLS.forEach(poll);
    connectEvents();
    render();
  });
})();
