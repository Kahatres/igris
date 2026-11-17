// Offline behavioral tests with a minimal DOM and the vendored geometry library.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../igris/static/', import.meta.url);
const source = await readFile(new URL('study.js', root), 'utf8');
const sceneSource = await readFile(new URL('study_scene.js', root), 'utf8');

class Node {
  constructor(tag = '#text', text = '') { this.tag = tag; this.tagName = tag.toUpperCase(); this.children = []; this.text = text; this.style = {}; this.dataset = {}; this.hidden = false; this.listeners = {}; }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return this.text + this.children.map(n => n.textContent).join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text = ''; this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  querySelectorAll() { return []; }
}
const document = { createElement: tag => new Node(tag), createTextNode: text => new Node('#text', text), querySelectorAll: () => [] };
const element = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
const formatSource = source.slice(source.indexOf('export function formatMessage'), source.indexOf('\nfunction message(')).replace('export ', '');
const getFormatter = math => new Function('document', 'MathLive', `${formatSource}; return formatMessage;`)(document, math);
const tags = node => [node.tag, ...node.children.flatMap(tags)];

test('minimal Markdown is formatted while HTML stays literal and code stays inert', () => {
  const node = new Node('div');
  getFormatter({})(node, '**Kalın** ve *eğik* ile `**kod** $x$`\nYeni satır\n1. Bir\n2. İki\n\n- Üç\n* Dört\n<img src=x onerror=alert(1)>');
  assert.ok(tags(node).includes('strong'));
  for (const tag of ['em', 'code', 'ol', 'ul', 'li', 'br']) assert.ok(tags(node).includes(tag), tag);
  assert.ok(node.textContent.includes('**kod** $x$'));
  assert.ok(node.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.ok(!tags(node).includes('img'));
  assert.equal(node.children.filter(n => n.tag === 'ol')[0].children.length, 2);
});

test('only dollar math spans reach static rendering; unavailable/failed rendering preserves text', () => {
  const calls = [];
  const node = new Node('div');
  getFormatter({ renderMathInElement: (span, options) => calls.push([span.textContent, options]) })(node, 'Hesap $x^2$ ve ` $y$ ` <b>metin</b>');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '$x^2$');
  assert.deepEqual(calls[0][1].TeX.delimiters.inline, [['$', '$']]);
  const failed = new Node('div');
  getFormatter({ renderMathInElement: span => { span.textContent = ''; throw Error(); } })(failed, '$x$');
  assert.equal(failed.textContent, '$x$');
  const unsafe = new Node('div');
  getFormatter({ renderMathInElement: () => assert.fail('unsafe math must stay text') })(unsafe, '$\\href{javascript:alert(1)}{x}$');
  assert.equal(unsafe.textContent, '$\\href{javascript:alert(1)}{x}$');
});

test('409 gives a Turkish preparation action with current step and hides practice editor', async () => {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
  const state = { checking: false };
  const sent = [];
  let missing = true;
  const api = async () => { if (missing) throw Object.assign(Error('missing'), { status: 409 }); return { id: 'p', statement: 'Soru', generated: true }; };
  const helpers = source.slice(source.indexOf('function setPracticeAvailable'), source.indexOf('\nfunction displayDate'));
  const chooseStep = new Function('document', '$', 'state', 'api', 'clearVerdict', 'addLine', 'sendChat', 'activateTab', 'element', `${helpers}; return chooseStep;`)(document, $, state, api, () => {}, () => {}, text => sent.push([text, state.step.id]), () => {}, element);
  await chooseStep({ id: 'step-1', goal: 'Çarpımı öğren', topic: 'Dot' });
  assert.equal($('practice-editor').hidden, true);
  assert.equal($('prepare-problem').hidden, false);
  assert.match($('problem').textContent, /soru/);
  await $('prepare-problem').onclick();
  assert.equal(sent[0][1], 'step-1');
  assert.match(sent[0][0], /Dot/);
  missing = false;
  await chooseStep(state.step);
  assert.equal($('practice-editor').hidden, false);
  assert.equal($('prepare-problem').hidden, true);
  assert.equal(state.problem.id, 'p');
});

const pureScene = sceneSource.slice(sceneSource.indexOf('export function sceneFrame'), sceneSource.indexOf('export function createScene'));
const THREE = await import('data:text/javascript;base64,' + Buffer.from(await readFile(new URL('vendor/three/three.module.min.js', root), 'utf8')).toString('base64'));
const sceneFrame = new Function('THREE', `${pureScene.replaceAll('export ', '')}; return sceneFrame;`)(THREE);

test('scene framing scales and centers tiny, large, translated, planar and empty scenes', () => {
  const frame = scale => sceneFrame({ objects: [{ type: 'vector', from: [0, 0, 0], to: [scale, 2 * scale, 3 * scale] }] });
  const tiny = frame(.01), big = frame(1000);
  assert.ok(Math.abs(big.scale / tiny.scale - 100000) < 1e-6);
  assert.ok(Math.abs(big.gridSize / tiny.gridSize - 100000) < 1e-6);
  const translated = sceneFrame({ objects: [{ type: 'line', from: [100, 200, 300], to: [102, 204, 306] }] });
  assert.deepEqual(translated.center.toArray(), [101, 202, 303]);
  const plane = sceneFrame({ objects: [{ type: 'plane', point: [10, 10, 10], normal: [1, 1, 1], size: 20 }] });
  assert.ok(plane.scale >= 20);
  for (const scene of [{ objects: [] }, { objects: [{ type: 'point', at: [0, 0, 0] }] }]) {
    const result = sceneFrame(scene);
    assert.ok(Number.isFinite(result.scale) && result.scale > 0);
    assert.ok(result.center.toArray().every(Number.isFinite));
  }
});


const computeReadouts = new Function(`${sceneSource.slice(sceneSource.indexOf('const EPSILON'), sceneSource.indexOf('export function sceneFrame')).replaceAll('export ', '')}; return computeReadouts;`)();
const cameraFitDistance = new Function('THREE', `${pureScene.replaceAll('export ', '')}; return cameraFitDistance;`)(THREE);

test('readouts name referenced object labels for every quantity, including duplicate norms', () => {
  const scene = { objects: [
    { id: 'v1', label: 'a', type: 'vector', from: [0, 0], to: [3, 0] },
    { id: 'v2', label: 'β', type: 'vector', from: [0, 0], to: [0, 4] },
  ], readouts: [
    { op: 'norm', ids: ['v1'], label: 'Old generic label' },
    { op: 'norm', vector: 'v2' },
    ...['dot', 'cross', 'angle', 'projection'].map(op => ({ op, vectors: ['v1', 'v2'] })),
  ] };
  const results = computeReadouts(scene);
  assert.deepEqual(results.map(r => r.label), ['Uzunluk |a|', 'Uzunluk |β|', 'Skaler çarpım a·β', 'Vektörel çarpım a×β', 'Açı (a, β)', 'İzdüşüm (a, β)']);
  assert.deepEqual(results.slice(0, 3).map(r => r.value), [3, 4, 0]);
  assert.equal(results[4].text, '90°');
  scene.objects[1].label = '';
  assert.equal(computeReadouts(scene)[1].label, 'Uzunluk |v2|');
});

test('camera uses vector bounds and fills 60% across wide and narrow canvases', () => {
  const frame = sceneFrame({ objects: [
    { type: 'vector', from: [0, 0, 0], to: [3, 1, 0] },
    { type: 'vector', from: [0, 0, 0], to: [1, 3, 0] },
    { type: 'plane', size: 700, point: [0, 0, 0] },
  ] });
  assert.equal(frame.scale, 3);
  for (const aspect of [3.5, 1, .6]) {
    const camera = new THREE.PerspectiveCamera(43, aspect, .001, 10000);
    camera.up.set(0, 0, 1);
    camera.position.copy(frame.center).add(new THREE.Vector3(1.3, -1.6, 1.2).normalize().multiplyScalar(cameraFitDistance(frame, 43, aspect)));
    camera.lookAt(frame.center);
    camera.updateMatrixWorld();
    const projected = [];
    for (const x of [0, 3]) for (const y of [0, 3]) projected.push(new THREE.Vector3(x, y, 0).project(camera));
    const extent = Math.max(...projected.flatMap(p => [Math.abs(p.x), Math.abs(p.y)]));
    assert.ok(Math.abs(extent - .6) < 1e-6, `${aspect}: ${extent}`);
  }
});

function chatHarness(api) {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
  $('messages').scrollHeight = 900;
  $('messages').scrollTop = 140;
  $('messages').scrollIntoView = () => assert.fail('must not scroll the page');
  const helpers = source.slice(source.indexOf('function scrollChat'), source.indexOf('async function sendChat'));
  return { $, ...new Function('$', 'element', 'formatMessage', 'api', `${helpers}; return { message, loadHistory, scrollChat };`)($, element, getFormatter({}), api) };
}

test('history preserves order and roles, uses live formatting, and scrolls only the chat', async () => {
  const harness = chatHarness(async path => {
    assert.equal(path, '/api/study/history');
    return { messages: [{ role: 'user', text: '**Soru**' }, { role: 'assistant', text: '*Yanıt* $x$' }, { role: 'system', text: 'Skip' }] };
  });
  await harness.loadHistory();
  const panel = harness.$('messages');
  assert.equal(panel.children.length, 2);
  assert.equal(panel.children[0].className, 'message user');
  assert.equal(panel.children[1].className, 'message assistant');
  assert.ok(tags(panel).includes('strong'));
  assert.ok(tags(panel).includes('em'));
  assert.equal(panel.scrollTop, 900);
  harness.message('Yeni yanıt');
  assert.equal(panel.children.at(-1).textContent, 'Yeni yanıt');
});

test('empty or failed history keeps the workspace greeting usable', async () => {
  for (const api of [async () => ({ messages: [] }), async () => { throw Error('offline'); }]) {
    const harness = chatHarness(api);
    harness.$('messages').append(new Node('p', 'Hoş geldin'));
    await harness.loadHistory();
    assert.equal(harness.$('messages').textContent, 'Hoş geldin');
    harness.message('Devam');
    assert.equal(harness.$('messages').children.length, 2);
  }
});

test('fixture history is empty and never accesses the network', async () => {
  const helpers = source.slice(source.indexOf('async function fixtureResponse'), source.indexOf('async function api'));
  const fixtureResponse = new Function('fixtureData', 'status', 'clone', `${helpers}; return fixtureResponse;`)(async () => ({}), () => {}, structuredClone);
  assert.deepEqual(await fixtureResponse('/api/study/history'), { messages: [] });
});

test('plan titles override topic labels with topic fallback', async () => {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
  const steps = [{ id: 'one', title: 'Özel adım', topic: 'Genel konu', goal: 'Hedef' }, { id: 'two', topic: 'İkinci konu', goal: 'Hedef' }];
  const helper = source.slice(source.indexOf('async function load()'), source.indexOf('async function initialize()'));
  const load = new Function('$', 'api', 'element', 'drawMastery', 'status', 'chooseStep', 'displayDate', 'state', `${helper}; return load;`)($, async () => ({ current: { exam: { name: 'Sınav' }, steps } }), element, () => {}, () => {}, async () => {}, () => '', {});
  await load();
  assert.equal($('steps').children[0].children[0].children[0].textContent, '1 · Özel adım');
  assert.equal($('steps').children[1].children[0].children[0].textContent, '2 · İkinci konu');
});

test('editing preserves verdict, explanation and attempt until the next check', () => {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
  const state = { revision: 0, attempt: 'previous' };
  $('verdict').textContent = 'İlk hata 1. satırda.';
  $('explanation').textContent = 'Bileşenleri kontrol et.';
  $('explain').hidden = false;
  const helpers = source.slice(source.indexOf('function markEdited'), source.indexOf('function renumber'));
  const { markEdited, clearVerdict } = new Function('$', 'state', 'document', `${helpers}; return { markEdited, clearVerdict };`)($, state, document);
  markEdited();
  assert.equal(state.revision, 1);
  assert.equal(state.attempt, 'previous');
  assert.equal($('explanation').textContent, 'Bileşenleri kontrol et.');
  assert.equal($('verdict').textContent, 'İlk hata 1. satırda.');
  clearVerdict();
  assert.equal(state.attempt, null);
  assert.equal($('explanation').textContent, '');
  assert.equal($('verdict').textContent, '');
});

test('explanation requested before an edit still appears under the same verdict', async () => {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
  const state = { revision: 0, attempt: 'a' };
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const helper = source.slice(source.indexOf("$('explain').addEventListener"), source.indexOf('function updateReadouts'));
  new Function('$', 'state', 'api', helper)($, state, async () => pending);
  const requested = $('explain').listeners.click();
  state.revision++;
  resolve({ reason: 'Neden', should_be: '32' });
  await requested;
  assert.match($('explanation').textContent, /Neden/);
  assert.equal($('explain').disabled, false);
});

test('check clears previous feedback and scrolls chat without moving the editor', async () => {
  const nodes = new Map();
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
  const row = new Node('li');
  row.querySelector = () => ({ getValue: () => '31' });
  row.classList = { add: name => { row.mark = name; } };
  $('solution-lines').append(row);
  $('solution-panel').scrollTop = 150;
  $('messages').scrollHeight = 900;
  $('explanation').textContent = 'Previous reason';
  const state = { problem: { id: 'p' }, revision: 0, fixture: false };
  const cleared = () => { state.revision++; state.attempt = null; $('explanation').textContent = ''; };
  const helper = source.slice(source.indexOf("$('check').addEventListener"), source.indexOf("$('explain').addEventListener"));
  new Function('$', 'state', 'api', 'clearVerdict', 'refreshMastery', 'scrollChat', helper)($, state,
    async (path, body) => {
      assert.equal(path, '/api/study/attempt');
      assert.deepEqual(body, { problem_id: 'p', lines: ['31'] });
      assert.equal($('explanation').textContent, '');
      return { verdict: 'wrong', line: 1, attempt_id: 'next' };
    }, cleared, () => {}, () => { $('messages').scrollTop = $('messages').scrollHeight; });
  await $('check').listeners.click();
  assert.equal($('solution-panel').scrollTop, 150);
  assert.equal($('messages').scrollTop, 900);
  assert.equal(state.attempt, 'next');
  assert.equal(row.mark, 'wrong');
  assert.equal($('verdict').dataset.verdict, 'wrong');
});

test('missing history endpoint does not enable fixtures or erase an attempt', async () => {
  const state = { fixture: false, attempt: 'saved' };
  const helper = source.slice(source.indexOf('async function api('), source.indexOf('function activateTab'));
  const api = new Function('state', 'fetch', 'fixtureResponse', '$', 'location', helper + '; return api;')(state,
    async () => ({ status: 404, ok: false }), () => assert.fail('history must not enable fixtures'),
    () => assert.fail('history failure must not mutate the workspace'), {});
  await assert.rejects(api('/api/study/history'));
  assert.equal(state.fixture, false);
  assert.equal(state.attempt, 'saved');
});

test('progress polls one request at a time, translates actual phases, and ignores late responses', async () => {
  const helper = source.slice(source.indexOf('function watchStudyProgress'), source.indexOf('async function sendChat'));
  const node = new Node('div', 'Yanıt hazırlanıyor…');
  const requests = [], timers = [], cancelled = [];
  let finish, scrolls = 0;
  const api = path => { requests.push(path); return new Promise(resolve => { finish = resolve; }); };
  const watch = new Function('state', 'api', 'scrollChat', 'setTimeout', 'clearTimeout', `${helper}; return watchStudyProgress;`)(
    { fixture: false }, api, () => scrolls++, (callback, delay) => { timers.push({ callback, delay }); return timers.length; }, id => cancelled.push(id));
  const stop = watch(node, 'one');
  assert.deepEqual(requests, ['/api/study/progress?request_id=one']);
  assert.equal(timers.length, 0);
  finish({ request_id: 'one', active: true, phase: 'reading' });
  await Promise.resolve();
  assert.equal(node.textContent, 'Notlar okunuyor…');
  assert.equal(timers[0].delay, 750);
  const pending = timers[0].callback();
  assert.equal(requests.length, 2);
  stop();
  node.textContent = 'Son yanıt';
  finish({ request_id: 'one', active: true, phase: 'graph' });
  await pending;
  assert.equal(node.textContent, 'Son yanıt');
  assert.equal(timers.length, 1);
  assert.equal(scrolls, 1);
  assert.deepEqual(cancelled, [1]);
});

test('progress ignores another turn and fixtures never poll', async () => {
  const helper = source.slice(source.indexOf('function watchStudyProgress'), source.indexOf('async function sendChat'));
  const node = new Node('div', 'Bekleniyor');
  let calls = 0;
  const state = { fixture: false };
  const watch = new Function('state', 'api', 'scrollChat', 'setTimeout', 'clearTimeout', `${helper}; return watchStudyProgress;`)(
    state, async () => { calls++; return { request_id: 'old', active: true, phase: 'graph' }; },
    () => assert.fail('unrelated turn must not scroll'), () => 1, () => {});
  const stop = watch(node, 'new');
  await Promise.resolve();
  assert.equal(node.textContent, 'Bekleniyor');
  stop();
  state.fixture = true;
  watch(node, 'fixture')();
  assert.equal(calls, 1);
});

test('chat shows a pending bubble immediately and replaces it on success or failure', async () => {
  const helper = source.slice(source.indexOf('async function sendChat'), source.indexOf("$('chat-form').addEventListener"));
  for (const fail of [false, true]) {
    const nodes = new Map();
    const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
    const state = { step: { id: 'step' }, problem: { id: 'problem' } };
    let finish, reject, stops = 0, watched;
    const api = async (path, body) => {
      assert.equal(path, '/api/study/chat');
      assert.equal(body.request_id, watched);
      return new Promise((resolve, no) => { finish = resolve; reject = no; });
    };
    const sendChat = new Function('state', '$', 'element', 'message', 'scrollChat', 'watchStudyProgress', 'api', 'formatMessage', 'showScene', `${helper}; return sendChat;`)(
      state, $, element, text => $('messages').append(element('div', text)), () => {},
      (node, requestId) => { watched = requestId; assert.equal(node['aria-busy'], 'true'); return () => stops++; }, api,
      (node, text) => node.append(element('span', text)), () => {});
    const request = sendChat('Sor');
    assert.equal($('messages').children.length, 2);
    const bubble = $('messages').children[1];
    assert.equal(bubble.textContent, 'Yanıt hazırlanıyor…');
    assert.equal($('send-chat').disabled, true);
    if (fail) reject(Error('Bağlantı kesildi'));
    else finish({ reply: 'Hazır.' });
    await request;
    assert.equal(bubble.textContent, fail ? 'Bağlantı kesildi' : 'Hazır.');
    assert.equal($('messages').children.length, 2);
    assert.equal(bubble['aria-busy'], undefined);
    assert.equal($('send-chat').disabled, false);
    assert.equal(state.chatting, false);
    assert.ok(stops >= 1);
  }
});

test('missing progress endpoint never enables fixtures', async () => {
  const state = { fixture: false, attempt: 'saved' };
  const helper = source.slice(source.indexOf('async function api('), source.indexOf('function activateTab'));
  const api = new Function('state', 'fetch', 'fixtureResponse', '$', 'location', helper + '; return api;')(state,
    async () => ({ status: 404, ok: false }), () => assert.fail('progress must not enable fixtures'),
    () => assert.fail('progress must not mutate the workspace'), {});
  await assert.rejects(api('/api/study/progress?request_id=one'));
  assert.equal(state.fixture, false);
  assert.equal(state.attempt, 'saved');
});
