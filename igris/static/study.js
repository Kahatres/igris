import * as MathLive from './vendor/mathlive/mathlive.min.mjs';
import { createScene } from './study_scene.js';

const { MathfieldElement } = MathLive;

// Configure the vendored editor before connecting any math-field to the document.
MathfieldElement.computeEngine = null;
MathfieldElement.fontsDirectory = new URL('./vendor/mathlive/fonts', import.meta.url).href;
MathfieldElement.soundsDirectory = null;
MathfieldElement.keypressSound = null;
MathfieldElement.plonkSound = null;
MathfieldElement.keypressVibration = false;
MathfieldElement.locale = 'tr';
MathfieldElement.speechEngine = 'local';

const $ = id => document.getElementById(id);
const state = { fixture: new URLSearchParams(location.search).get('fixture') === '1', step: null, problem: null, selected: null, scene: null, attempt: null, revision: 0, checking: false, chatting: false };
let fixturePromise, activeField, scene, authPromise;
async function csrfToken() {
  authPromise ??= fetch('/api/auth/me', { credentials: 'same-origin' }).then(async response => {
    if (response.status === 401) location.replace('/login');
    if (!response.ok) throw new Error('Oturum bilgisi alınamadı.');
    return (await response.json()).csrf ?? '';
  }).catch(error => { authPromise = null; throw error; });
  return authPromise;
}
const clone = value => JSON.parse(JSON.stringify(value));
const dates = new Intl.DateTimeFormat('tr-TR', { day: 'numeric', month: 'long', year: 'numeric' });

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function status(text) { $('connection').textContent = text; }
function fixtureData() {
  fixturePromise ??= fetch(new URL('./study_fixture.json', import.meta.url), { credentials: 'same-origin' }).then(async response => {
    if (!response.ok) throw new Error();
    return await response.json();
  }).catch(() => {
    fixturePromise = null;
    throw new Error('Örnek çalışma verisi yüklenemedi.');
  });
  return fixturePromise;
}

async function fixtureResponse(path, body) {
  const fixture = await fixtureData();
  status('Örnek çalışma · Sonuçlar kaydedilmez');
  if (path === '/api/study') return clone(fixture.overview);
  if (path === '/api/study/history') return { messages: [] };
  if (path.startsWith('/api/study/problem?')) return clone(fixture.problem);
  if (path === '/api/study/attempt') {
    const final = body.lines.at(-1).replace(/\s|\\left|\\right/g, '');
    return clone(fixture.check.correct_finals.includes(final) ? fixture.check.right : fixture.check.wrong);
  }
  if (path === '/api/study/explain') return clone(fixture.check.explanation);
  if (path === '/api/study/chat') {
    const text = body.text.toLocaleLowerCase('tr-TR');
    if (/izdüş|projeks/.test(text)) return { reply: fixture.chat.projection, scene: clone(fixture.scenes[1]) };
    if (/grafik|sahne|çiz|göster/.test(text)) return { reply: fixture.chat.graph, scene: clone(fixture.scenes[0]) };
    return { reply: body.selected ? `${body.selected.label} seçili. ${fixture.chat.selected}` : fixture.chat.offer, scene: null };
  }
  throw new Error('Bu işlem örnek çalışmada bulunmuyor.');
}

async function api(path, body) {
  if (state.fixture) return fixtureResponse(path, body);
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': await csrfToken() },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch { throw new Error('Bağlantı kurulamadı. Bağlantıyı kontrol edip yeniden dene.'); }
  if (response.status === 404 && path !== '/api/study/history' && !path.startsWith('/api/study/progress')) {
    state.fixture = true;
    state.attempt = null;
    $('explain').hidden = true;
    return fixtureResponse(path, body);
  }
  if (response.status === 401) { location.replace('/login'); throw new Error('Oturum açmalısın.'); }
  if (response.status === 409) throw Object.assign(new Error('Bu adım için henüz soru hazırlanmadı.'), { status: 409 });
  if (!response.ok) throw new Error(response.status === 403 ? 'Bu çalışma alanına erişim iznin yok.' : 'İşlem tamamlanamadı. Yeniden dene.');
  try { return await response.json(); }
  catch { throw new Error('Sunucunun yanıtı okunamadı. Yeniden dene.'); }
}

function activateTab(name, focus = false) {
  document.querySelector('.workspace').dataset.active = name;
  if (name === 'chat') scrollChat();
  document.querySelectorAll('[data-tab]').forEach(button => {
    const active = button.dataset.tab === name;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    if (active && focus) button.focus();
  });
}
document.querySelectorAll('[data-tab]').forEach((button, index, buttons) => {
  button.addEventListener('click', () => activateTab(button.dataset.tab));
  button.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
    if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = buttons.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    activateTab(buttons[next].dataset.tab, true);
  });
});

function markEdited() {
  state.revision++;
}

function clearVerdict() {
  markEdited();
  state.attempt = null;
  $('explain').hidden = true;
  $('verdict').textContent = '';
  $('verdict').removeAttribute('data-verdict');
  $('explanation').textContent = '';
  document.querySelectorAll('.solution-lines .wrong').forEach(line => line.classList.remove('wrong'));
}

function renumber() {
  document.querySelectorAll('#solution-lines li').forEach((row, index) => {
    row.querySelector('math-field').setAttribute('aria-label', `Çözüm satırı ${index + 1}`);
    row.querySelector('button').setAttribute('aria-label', `${index + 1}. satırı sil`);
  });
}

function addLine(after = null, focus = true) {
  const row = element('li');
  const field = new MathfieldElement();
  field.mathVirtualKeyboardPolicy = 'manual';
  field.menuItems = [];
  field.defaultMode = 'math';
  field.setAttribute('lang', 'tr');
  field.setAttribute('aria-label', 'Çözüm satırı');
  field.addEventListener('focusin', () => { activeField = field; });
  field.addEventListener('input', markEdited);
  field.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      event.stopPropagation();
      addLine(row);
    }
  }, true);
  const remove = element('button', '×', 'quiet remove-line');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    const next = row.nextElementSibling ?? row.previousElementSibling;
    row.remove();
    markEdited();
    if (!$('solution-lines').children.length) addLine();
    else next.querySelector('math-field').focus();
    renumber();
  });
  row.append(field, remove);
  if (after) after.after(row);
  else $('solution-lines').append(row);
  renumber();
  markEdited();
  if (focus) field.focus();
  return field;
}

const palette = [
  ['Kesir', 'a/b', '\\frac{#0}{#?}'], ['Karekök', '√', '\\sqrt{#0}'],
  ['Vektör oku', 'a⃗', '\\vec{#0}'], ['Skaler çarpım', '·', '\\cdot'],
  ['Vektörel çarpım', '×', '\\times'], ['Norm', '‖a‖', '\\left\\lVert #0 \\right\\rVert'],
  ['Matris', '▦', '\\begin{pmatrix}#0 & #? \\\\ #? & #?\\end{pmatrix}'],
];
palette.forEach(([label, text, latex]) => {
  const button = element('button', text);
  button.type = 'button';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.addEventListener('click', () => {
    if (!activeField?.isConnected) activeField = $('solution-lines').querySelector('math-field');
    activeField.focus();
    activeField.insert(latex, { selectionMode: 'placeholder' });
    markEdited();
  });
  $('palette').append(button);
});
$('add-line').addEventListener('click', () => addLine());
addLine(null, false);

$('check').addEventListener('click', async () => {
  if (!state.problem || state.checking) return;
  const rows = [...$('solution-lines').children];
  const lines = rows.map(row => row.querySelector('math-field').getValue('latex'));
  if (!lines.some(line => line.trim())) { $('verdict').textContent = 'Önce çözümünü yaz.'; return; }
  if (lines.some(line => !line.trim())) { $('verdict').textContent = 'Boş satırları doldur veya sil.'; return; }
  clearVerdict();
  const revision = state.revision;
  state.checking = true;
  $('check').disabled = true;
  $('check').textContent = 'Kontrol ediliyor…';
  try {
    const result = await api('/api/study/attempt', { problem_id: state.problem.id, lines });
    if (state.revision !== revision) return;
    const validLine = Number.isInteger(result.line) && result.line >= 1 && result.line <= rows.length;
    const verdict = ['right', 'wrong', 'unsure'].includes(result.verdict) && (result.verdict !== 'wrong' || validLine) ? result.verdict : 'unsure';
    $('verdict').dataset.verdict = verdict;
    const prefix = state.fixture ? 'Örnek yanıt: ' : '';
    $('verdict').textContent = prefix + ({ right: 'Çözümün doğru.', wrong: `İlk hata ${result.line}. satırda. Yeniden dene veya nedenini sor.`, unsure: 'Emin değilim. Bu çözüm güvenilir biçimde doğrulanamadı; doğru kabul edilmedi.' })[verdict]
      + (result.verified === false && verdict !== 'unsure' ? ' Bu yalnızca modelin değerlendirmesi; hesapla doğrulanmadı.' : '');
    if (verdict === 'wrong') rows[result.line - 1].classList.add('wrong');
    state.attempt = result.attempt_id ?? null;
    $('explain').hidden = !state.attempt || verdict === 'right';
    refreshMastery();
  } catch (error) { if (revision === state.revision) $('verdict').textContent = error.message; }
  finally { scrollChat(); state.checking = false; $('check').disabled = !state.problem; $('check').textContent = 'Kontrol et'; }
});

$('explain').addEventListener('click', async () => {
  if (!state.attempt) return;
  const attempt = state.attempt;
  $('explain').disabled = true;
  try {
    const result = await api('/api/study/explain', { attempt_id: attempt });
    if (state.attempt !== attempt) return;
    $('explanation').textContent = [result.reason, result.should_be ? `Olması gereken: ${result.should_be}` : ''].filter(Boolean).join('\n');
  } catch (error) { if (state.attempt === attempt) $('explanation').textContent = error.message; }
  finally { $('explain').disabled = false; }
});

function updateReadouts() {
  $('readouts').replaceChildren();
  scene?.readouts().forEach(readout => {
    const entry = element('div');
    entry.append(element('dt', readout.label), element('dd', readout.text));
    $('readouts').append(entry);
  });
}

function updateCoordinates() {
  $('vector-controls').replaceChildren();
  const object = state.scene?.objects.find(item => item.id === state.selected?.id);
  if (object?.type !== 'vector' || !object.draggable) return;
  ['x', 'y', 'z'].forEach((axis, index) => {
    const label = element('label', `${object.label || object.id} · ${axis}`);
    const input = document.createElement('input');
    input.type = 'number'; input.step = '.5'; input.value = object.to[index] ?? 0;
    input.setAttribute('aria-label', `${object.label || object.id} ucunun ${axis} koordinatı`);
    input.addEventListener('change', () => {
      const value = input.valueAsNumber;
      if (!Number.isFinite(value) || Math.abs(value) > 10000) { input.value = object.to[index] ?? 0; return; }
      object.to = [object.to[0], object.to[1], object.to[2] ?? 0];
      object.to[index] = Math.round(value * 2) / 2;
      input.value = object.to[index];
      scene.show(state.scene);
      updateReadouts();
    });
    label.append(input);
    $('vector-controls').append(label);
  });
}

function selectObject(selected) {
  state.selected = selected;
  $('selection').textContent = selected ? `Seçili: ${selected.label} · Sonraki soruna eklenecek.` : 'Bir nesne seç; sonraki sorunda onu konuşalım.';
  $('scene-objects').querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.id === selected?.id)));
  updateCoordinates();
}

function showScene(data) {
  if (!scene) { status('Üç boyutlu sahne kullanılamıyor; plan, sohbet ve çözüm açık.'); return; }
  scene.show(data);
  state.scene = clone(data);
  $('scene-title').textContent = data.title || 'Vektör sahnesi';
  $('scene-objects').replaceChildren();
  data.objects.forEach(object => {
    const button = element('button', object.label || object.id);
    button.type = 'button'; button.dataset.id = object.id;
    button.setAttribute('aria-label', `${object.label || object.id} nesnesini seç`);
    button.addEventListener('click', () => $('scene').dispatchEvent(new CustomEvent('study-select', { detail: object.id })));
    $('scene-objects').append(button);
  });
  selectObject(null);
  $('scene').dispatchEvent(new CustomEvent('study-select', { detail: null }));
  updateReadouts();
}

try {
  scene = createScene($('scene'));
  scene.onSelect(selectObject);
  scene.onChange(data => { state.scene = data; updateReadouts(); updateCoordinates(); });
} catch {
  $('scene').append(element('p', 'Bu tarayıcı üç boyutlu sahneyi açamadı. WebGL destekli bir tarayıcıyla yeniden dene.', 'message'));
}

export function formatMessage(node, text) {
  function inline(parent, value, depth = 0) {
    // Only create known DOM nodes; source HTML is always a text node.
    const tokens = /`([^`\n]+)`|\$([^$\n]+)\$|\*\*([^\n]+?)\*\*|\*([^*\n]+)\*|_([^_\n]+)_/g;
    let start = 0;
    for (const match of value.matchAll(tokens)) {
      parent.append(document.createTextNode(value.slice(start, match.index)));
      const tag = match[1] !== undefined ? 'code' : match[2] !== undefined ? 'span' : match[3] !== undefined ? 'strong' : 'em';
      const child = document.createElement(tag);
      const content = match[1] ?? match[2] ?? match[3] ?? match[4] ?? match[5];
      if (tag === 'span') {
        child.textContent = match[0];
        // Leave HTML/link/style commands literal, including inside math spans.
        if (typeof MathLive.renderMathInElement === 'function' && !/[<>]|\\(?:href|url|html\w*|includegraphics|style|class|cssId)\b/i.test(content)) {
          try {
            MathLive.renderMathInElement(child, {
              renderAccessibleContent: '',
              TeX: { processEnvironments: false, delimiters: { inline: [['$', '$']], display: [] } },
            });
          } catch { child.textContent = match[0]; }
        }
      } else if (tag === 'code' || depth >= 2) child.textContent = content;
      else inline(child, content, depth + 1);
      parent.append(child);
      start = match.index + match[0].length;
    }
    parent.append(document.createTextNode(value.slice(start)));
  }
  let list = null, previousLine = false;
  for (const line of String(text).split(/\r?\n/)) {
    const match = line.match(/^\s*(?:(\d+)[.)]|([-+*]))\s+(.+)$/);
    if (match) {
      const tag = match[1] ? 'ol' : 'ul';
      if (!list || list.tagName?.toLowerCase() !== tag) {
        list = document.createElement(tag);
        if (match[1]) list.setAttribute('start', match[1]);
        node.append(list);
      }
      const item = document.createElement('li');
      inline(item, match[3]);
      list.append(item);
    } else {
      if (previousLine && !list) node.append(document.createElement('br'));
      list = null;
      inline(node, line);
    }
    previousLine = true;
  }
}

function scrollChat() {
  const panel = $('messages');
  panel.scrollTop = panel.scrollHeight;
}

function message(text, role = 'assistant') {
  const node = element('div', undefined, `message ${role}`);
  formatMessage(node, text);
  $('messages').append(node);
  scrollChat();
}

async function loadHistory() {
  try {
    const history = await api('/api/study/history');
    const messages = (history.messages ?? []).filter(item =>
      ['user', 'assistant'].includes(item.role) && typeof item.text === 'string');
    if (messages.length) {
      $('messages').replaceChildren();
      messages.forEach(item => message(item.text, item.role));
    }
  } catch { /* History is optional; the workspace stays usable. */ }
}

function watchStudyProgress(node, requestId) {
  if (state.fixture) return () => {};
  let stopped = false, timer;
  const phases = {
    thinking: 'Yanıt hazırlanıyor…', reading: 'Notlar okunuyor…',
    planning: 'Plan kuruluyor…', graph: 'Grafik hazırlanıyor…',
    calendar: 'Takvim kontrol ediliyor…', problem: 'Alıştırma hazırlanıyor…',
  };
  async function poll() {
    try {
      const progress = await api(`/api/study/progress?request_id=${encodeURIComponent(requestId)}`);
      if (!stopped && progress.request_id === requestId && progress.active) {
        node.textContent = phases[progress.phase] ?? phases.thinking;
        scrollChat();
      }
    } catch { /* Progress is optional; the chat request still owns its outcome. */ }
    if (!stopped) timer = setTimeout(poll, 750);
  }
  poll();
  return () => { stopped = true; clearTimeout(timer); };
}

async function sendChat(text) {
  if (state.chatting) return;
  state.chatting = true;
  $('send-chat').disabled = true;
  $('messages').querySelectorAll('.graph-offer').forEach(button => { button.disabled = true; });
  const selected = state.selected ? { ...state.selected } : null, step = state.step?.id ?? null;
  message(text, 'user');
  const pending = element('div', 'Yanıt hazırlanıyor…', 'message assistant');
  pending.setAttribute('role', 'status');
  pending.setAttribute('aria-live', 'polite');
  pending.setAttribute('aria-busy', 'true');
  $('messages').append(pending);
  scrollChat();
  const requestId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const stopProgress = watchStudyProgress(pending, requestId);
  try {
    const result = await api('/api/study/chat', { text, selected, step, request_id: requestId });
    stopProgress();
    if (typeof result.reply !== 'string') throw new Error('Sohbet yanıtı okunamadı.');
    pending.replaceChildren();
    formatMessage(pending, result.reply);
    if (!state.step) await load();
    else if (!state.problem) await chooseStep(state.step);
    if (result.scene) showScene(result.scene);
    else if (/grafik|görsel|sahne|çizim/i.test(result.reply)) {
      const offer = element('button', 'Grafikle göster ↗', 'graph-offer');
      offer.type = 'button';
      offer.addEventListener('click', () => sendChat('Bu konuyu grafikle göster.'));
      $('messages').append(offer);
    }
  } catch (error) { pending.textContent = error.message; }
  finally {
    stopProgress();
    pending.removeAttribute('aria-busy');
    scrollChat();
    state.chatting = false;
    $('send-chat').disabled = false;
    $('messages').querySelectorAll('.graph-offer').forEach(button => { button.disabled = false; });
  }
}
$('chat-form').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('chat-input').value.trim();
  if (!text || state.chatting) return;
  $('chat-input').value = '';
  sendChat(text);
});

function setPracticeAvailable(available) {
  $('practice-editor').hidden = !available;
  $('prepare-problem').hidden = true;
}

let problemRequest = 0;
async function chooseStep(step) {
  state.step = step;
  state.problem = null;
  setPracticeAvailable(false);
  const request = ++problemRequest;
  $('check').disabled = true;
  clearVerdict();
  $('solution-lines').replaceChildren();
  addLine(null, false);
  $('problem').textContent = 'Soru yükleniyor…';
  $('problem-source').hidden = true;
  $('current-goal').textContent = step.goal;
  document.querySelectorAll('#steps button').forEach(button => {
    if (button.dataset.id === step.id) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  try {
    const problem = await api(`/api/study/problem?step=${encodeURIComponent(step.id)}`);
    if (request !== problemRequest) return;
    if (!problem.id || typeof problem.statement !== 'string') throw new Error('Bu adım için soru bulunamadı.');
    state.problem = problem;
    setPracticeAvailable(true);
    $('problem').textContent = problem.statement;
    $('problem-source').hidden = !problem.generated;
    $('check').disabled = state.checking;
  } catch (error) {
    if (request !== problemRequest) return;
    $('problem').textContent = error.message;
    if (error.status === 409) {
      $('problem').textContent = 'Bu adım için henüz soru hazırlanmadı. Igris’ten bu konuya uygun bir alıştırma iste.';
      const button = $('prepare-problem');
      button.hidden = false;
      button.onclick = () => {
        if (state.chatting || state.step?.id !== step.id) return;
        activateTab('chat');
        return sendChat(`Bu çalışma adımı için bir soru hazırla ve study_problem_add ile kaydet: ${step.topic}. Hedef: ${step.goal}. Örnek yoksa üretilmiş soru olarak işaretle; destekleniyorsa yapılandırılmış veriler ve hesaplanabilir yanıt ekle.`);
      };
    }
  }
}

function displayDate(value) {
  const date = new Date(value?.length === 10 ? `${value}T12:00:00` : value);
  return Number.isNaN(date.getTime()) ? 'Tarih belirlenmedi' : dates.format(date);
}

function drawMastery(list) {
  $('mastery').replaceChildren();
  (list ?? []).forEach(topic => {
    const item = element('li');
    const level = Math.max(0, Math.min(3, topic.level || 0));
    const meter = document.createElement('meter'); meter.min = 0; meter.max = 3; meter.value = level;
    meter.setAttribute('aria-label', `${topic.topic}: ${level}/3`);
    item.append(element('span', topic.topic), meter, element('small', `${topic.attempts ?? 0} deneme · ${topic.mistakes ?? 0} hata · ${topic.corrected ?? 0} düzeltme`));
    $('mastery').append(item);
  });
}

async function refreshMastery() {
  try { drawMastery((await api('/api/study')).current?.mastery); } catch (error) { /* the counts refresh on the next load */ }
}

async function load() {
  try {
    const overview = await api('/api/study');
    const current = overview.current;
    if (!current?.exam) {
      $('exam-name').textContent = 'Henüz sınav planı yok';
      $('current-goal').textContent = 'Igris ile sohbet ederek bir sınav ve çalışma planı oluştur.';
      $('problem').textContent = 'Bir çalışma adımı oluşturduğunda sorular burada görünecek.';
      status('Çalışma alanı hazır');
      return;
    }
    $('exam-name').textContent = current.exam.name;
    $('exam-date').textContent = `Sınav · ${displayDate(current.exam.date)}`;
    $('steps').replaceChildren();
    (current.steps ?? []).forEach((step, index) => {
      const item = element('li'), button = element('button');
      button.type = 'button'; button.dataset.id = step.id;
      const done = step.done || step.state === 'done';
      button.append(element('strong', `${done ? '✓' : index + 1} · ${step.title ?? step.topic}`), element('small', step.goal));
      if (step.start) button.append(element('small', displayDate(step.start)));
      button.addEventListener('click', () => chooseStep(step));
      item.append(button); $('steps').append(item);
    });
    drawMastery(current.mastery);
    if (!state.fixture) status('Çalışma alanı hazır');
    const next = current.steps?.find(step => !step.done && step.state !== 'done') ?? current.steps?.[0];
    if (next) await chooseStep(next);
    else $('problem').textContent = 'Henüz çalışma adımı yok. Sohbetten bir plan iste.';
    if (state.fixture) showScene((await fixtureData()).scenes[0]);
  } catch (error) { status(error.message); $('exam-name').textContent = 'Plan yüklenemedi'; $('problem').textContent = 'Bağlantıyı kontrol edip sayfayı yenile.'; }
}
async function initialize() {
  $('send-chat').disabled = true;
  await Promise.all([load(), loadHistory()]);
  $('send-chat').disabled = false;
}
initialize();
