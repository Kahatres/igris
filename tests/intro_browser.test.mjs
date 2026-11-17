// Offline geometry, renderer and lifecycle tests: no browser, network, model or CLI.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
const source = await readFile(new URL('../igris/static/intro.js', import.meta.url), 'utf8');
const artSource = await readFile(new URL('../igris/static/intro_art.js', import.meta.url), 'utf8');
const { buildRose, phaseAt, createRenderer, TIMELINE, startIntro, reviewTime, lowQuality, layout, PEN_COUNT, partialReveal, samplePen } =
  createRequire(import.meta.url)('../igris/static/intro.js');
const { burnTime, wordBox, buildParticles, sampleParticle, sampleDrop } = createRequire(import.meta.url)('../igris/static/intro_art.js');
function fakeCanvas() { const ctx = context(); return { getContext: () => ctx }; }

test('normalized rose strokes have ordered kinds, pressure, timing and bounded budgets at both details', () => {
  const kinds = ['gesture', 'contour', 'shading', 'details'], edges = [0.3, 1.2, 6, 6, 8.2];
  for (const low of [false, true]) {
    const r = buildRose(17, low);
    assert.ok(r.count > (low ? 1600 : 3400) && r.count < (low ? 2100 : 4100), r.count);
    assert.ok(r.totalLength > (low ? 30 : 50) && r.totalLength < (low ? 60 : 90), r.totalLength);
    let lastKind = 0, lastTime = 0, length = 0, segments = 0;
    for (const s of r.strokes) {
      const stage = kinds.indexOf(s.kind);
      assert.ok(stage >= lastKind); lastKind = stage;
      assert.ok(s.moveStart >= (s.pen >= 0 && r.schedule.pens[s.pen].strokeIds[0] === s.id ? 0.3 : edges[stage]) - 1e-10);
      assert.ok(s.start > s.moveStart && s.end > s.start);
      assert.ok(s.start >= edges[stage] && s.end <= (stage < 2 ? edges[stage + 1] : 8.2) + 1e-10);
      assert.ok(s.pressure > 0 && s.pressure <= 1 && s.weight > 0);
      assert.ok(s.points instanceof Float32Array && s.force instanceof Float32Array && s.grain instanceof Float32Array);
      assert.equal(s.force.length, s.points.length / 2);
      assert.equal(s.grain.length, s.force.length);
      assert.ok(s.force.every(v => Number.isFinite(v) && v >= 0 && v <= 1));
      assert.ok(s.points.every(v => Number.isFinite(v) && v > 0 && v < 1));
      assert.ok(s.force[0] < Math.max(...s.force) * 0.4, 'taper at start');
      assert.ok(s.force.at(-1) < Math.max(...s.force) * 0.4, 'taper at end');
      lastTime = Math.max(lastTime,s.end); length += s.length; segments += s.force.length - 1;
    }
    assert.ok(lastTime <= 8.2 && lastTime > 7.9); assert.equal(length, r.totalLength);
    assert.ok(segments < (low ? 34000 : 60000), segments);
    assert.equal(r.strokes.filter(s => s.kind === 'gesture').length, 8);
    assert.equal(r.strokes.filter(s => s.kind === 'contour').length, 69);
    for (const label of ['heart', 'rolled-rim', 'cross-hatch', 'dew', 'dew-highlight']) {
      assert.ok(r.strokes.some(s => s.label === label), label);
    }
    assert.equal(new Set(r.strokes.filter(s => s.label.startsWith('serrated-leaf')).map(s => s.label)).size, 3);
  }
  const high = buildRose(17, false), low = buildRose(17, true);
  for (const key of ['points', 'force', 'grain']) assert.deepEqual(low.strokes.filter(s => s.kind === 'contour').map(s => s[key]), high.strokes.filter(s => s.kind === 'contour').map(s => s[key]));
  assert.ok(low.count / high.count > 0.45 && low.count / high.count < 0.6);
  assert.ok(lowQuality(599, 16)); assert.ok(lowQuality(1200, 4));
  assert.ok(!lowQuality(600, 8)); assert.ok(!lowQuality(1200, undefined));
});

test('the same seed reproduces every stroke, pressure and grain sample', () => {
  for (const low of [false, true]) {
    const first = buildRose(1342, low), again = buildRose(1342, low), other = buildRose(1343, low);
    assert.deepEqual(first, again);
    assert.notDeepEqual(first.strokes[0].points, other.strokes[0].points);
    assert.notDeepEqual(first.strokes[0].grain, other.strokes[0].grain);
  }
});

test('the seeded schedule assigns every sketch stroke once, with finite nonoverlapping pen work', () => {
  assert.deepEqual(PEN_COUNT,{ desktop: 12, reduced: 8 });
  for (const low of [false,true]) for (const seed of [17,42,1342,137508]) {
    const rose = buildRose(seed,low), schedule = rose.schedule, seen = new Set();
    assert.equal(schedule.penCount,low ? PEN_COUNT.reduced : PEN_COUNT.desktop);
    assert.deepEqual(schedule,buildRose(seed,low).schedule);
    assert.ok(schedule.completionOrder instanceof Uint32Array);
    let lastEnd = 0;
    for (const id of schedule.completionOrder) {
      assert.ok(rose.strokes[id].end >= lastEnd); lastEnd = rose.strokes[id].end;
    }
    assert.equal(new Set(schedule.completionOrder).size,rose.count);
    for (const pen of schedule.pens) {
      assert.ok(pen.strokeIds instanceof Uint32Array && pen.travel instanceof Float64Array);
      assert.equal(pen.travel.length,pen.strokeIds.length*6);
      let previousEnd = 0;
      for (const id of pen.strokeIds) {
        const s = rose.strokes[id], stage = schedule.stages.find(stage => stage.kind === s.kind);
        assert.ok(!seen.has(id)); seen.add(id);
        assert.equal(s.pen,pen.id);
        assert.ok([s.moveStart,s.start,s.end].every(Number.isFinite));
        assert.ok(s.end > s.start && s.start > s.moveStart);
        assert.ok(s.moveStart >= previousEnd-1e-12, `pen ${pen.id} overlaps at ${id}`);
        assert.ok(Math.abs(s.arcLength/(s.end-s.start)-stage.speeds[pen.id]) < 1e-8);
        assert.equal(stage.travelSpeeds[pen.id],stage.speeds[pen.id]*schedule.travelRatio);
        previousEnd = s.end;
      }
    }
    assert.equal(seen.size,rose.strokes.filter(s => s.kind === 'gesture' || s.kind === 'contour').length);
    for (const [i,end] of [1.2,6].entries()) assert.ok(Math.abs(schedule.stages[i].end-end) < 1e-12);
    for (let stage = 1; stage < schedule.stages.length; stage++) {
      const previous = rose.strokes.filter(s => s.kind === schedule.stages[stage-1].kind);
      const current = rose.strokes.filter(s => s.kind === schedule.stages[stage].kind);
      const firstStart = Math.min(...current.map(s => s.start));
      assert.ok(firstStart >= Math.max(...previous.map(s => s.start))-0.3);
      assert.ok(firstStart >= Math.max(...previous.map(s => s.end))-1e-12);
    }
    assert.ok(!rose.strokes.some(s => s.label === 'signature'));
  }
});

test('at least 70 percent of the team draws at the quarter, half and three-quarter stage times', () => {
  for (const low of [false,true]) for (const seed of [17,42,1342,137508]) {
    const rose = buildRose(seed,low), out = new Float64Array(5);
    for (const stage of rose.schedule.stages.slice(1,3)) for (const progress of [0.25,0.5,0.75]) {
      const t = stage.start+(stage.end-stage.start)*progress;
      const drawing = rose.strokes.filter(s => s.start <= t && s.end > t);
      assert.ok(drawing.length >= Math.ceil(rose.schedule.penCount*0.7), `${low}/${seed}/${t}: ${drawing.length}`);
      assert.equal(new Set(drawing.map(s => s.pen)).size,drawing.length);
      for (const s of drawing) {
        samplePen(rose,s.pen,t,out); assert.equal(out[3],s.id);
        assert.ok(out[4] > 0 && out[4] < 1);
      }
    }
  }
});

test('every pen-up trip starts at the previous endpoint and ends at the next stroke without teleporting', () => {
  for (const low of [false,true]) {
    const rose = buildRose(137508,low), out = new Float64Array(5);
    for (const pen of rose.schedule.pens) {
      let x = pen.initialX, y = pen.initialY, previousEnd = 0;
      for (const [i,id] of pen.strokeIds.entries()) {
        const s = rose.strokes[id], p = s.points, travel = pen.travel.subarray(i*6,i*6+6);
        assert.deepEqual(Array.from(travel),[s.moveStart,s.start,x,y,p[0],p[1]]);
        for (const [t,expectedX,expectedY] of [
          [previousEnd,x,y], [s.moveStart,x,y],
          [(s.moveStart+s.start)/2,(x+p[0])/2,(y+p[1])/2],
          [s.start,p[0],p[1]], [s.end,p.at(-2),p.at(-1)]
        ]) {
          samplePen(rose,pen.id,t,out);
          assert.ok(Math.abs(out[0]-expectedX) < 1e-8 && Math.abs(out[1]-expectedY) < 1e-8, `${id} at ${t}`);
        }
        samplePen(rose,pen.id,s.start-(s.start-s.moveStart)*1e-5,out);
        assert.ok(Math.hypot(out[0]-p[0],out[1]-p[1]) < 1e-8);
        x = p.at(-2); y = p.at(-1); previousEnd = s.end;
      }
    }
  }
});

test('partial reveals are exact arc-length prefixes, including tiny dew strokes', () => {
  const rose = buildRose(137508,true), out = new Float64Array(4);
  // Unequal segments catch a knot-count reveal even when the authored art is densely sampled.
  const uneven = { points: new Float32Array([0,0,0.15,0,0.15,0.9]), lengths: new Float64Array([0,0.1,1]), arcLength: 1 };
  for (const s of [uneven,...rose.strokes]) {
    for (const progress of [0,0.01,0.25,0.5,0.9,1]) {
      assert.equal(partialReveal(s,progress,out),out);
      const segment = out[2], fraction = out[3], p = s.points;
      assert.ok(Number.isInteger(segment) && segment >= 0 && segment < s.lengths.length-1);
      assert.ok(fraction >= -1e-12 && fraction <= 1+1e-12);
      const revealed = s.lengths[segment]+fraction*(s.lengths[segment+1]-s.lengths[segment]);
      assert.ok(Math.abs(revealed-progress*s.arcLength) < 1e-12);
      assert.ok(Math.abs(out[0]-(p[segment*2]+fraction*(p[segment*2+2]-p[segment*2]))) < 1e-12);
      assert.ok(Math.abs(out[1]-(p[segment*2+1]+fraction*(p[segment*2+3]-p[segment*2+1]))) < 1e-12);
    }
  }
  partialReveal(uneven,0.5,out);
  assert.equal(out[2],1); assert.ok(Math.abs(out[3]-4/9) < 1e-12);
});

test('timeline includes the cluster, sweep, drop, whole burn, ash word and 14.8 cleanup', () => {
  assert.deepEqual([0,0.3,1.2,6,8.2,8.4,9.3,10.6,13.6,14,14.8,20].map(phaseAt),
    ['cluster','gesture','contour','sweep','hold','drop','burn','ash','word','reveal','done','done']);
  const values = Object.values(TIMELINE);
  assert.ok(values.every((v,i) => i === 0 || v >= values[i-1]));
  assert.equal(TIMELINE.reveal,14); assert.equal(TIMELINE.cardEnd,14.8); assert.equal(TIMELINE.end,14.8);
  const r = buildRose(18,false), samples = [];
  assert.equal(burnTime(...r.ignition),9.3);
  for (const s of r.strokes) for (let k = 0; k < s.points.length; k += 2) {
    const x = s.points[k], y = s.points[k+1], t = burnTime(x,y);
    assert.ok(Number.isFinite(t) && t >= 9.3 && t+0.5 < 11.6, `${s.label}: ${t}`);
    if (s.part) samples.push([Math.hypot((x-r.ignition[0])*600,(y-r.ignition[1])*900),t]);
  }
  samples.sort((a,b) => a[0]-b[0]);
  for (let i = 1; i < samples.length; i++) assert.ok(samples[i][1] >= samples[i-1][1]);
  // Travel down the stem, then branch along the leaves, rather than ignite leaf tips early.
  assert.ok(burnTime(0.5,0.6) < burnTime(0.2,0.6));
  assert.ok(burnTime(0.5,0.6) < burnTime(0.5,0.8));
});

test('review hash accepts finite nonnegative seconds and rejects ambiguous hashes', () => {
  for (const [hash, expected] of [['#t=0', 0], ['#t=6.3', 6.3], ['#t=20', 20],
    ['', null], ['#t=-1', null], ['#t=NaN', null], ['#t=Infinity', null],
    ['#t=2.6&x=1', null], ['#t=1e2', null], ['#t=' + '9'.repeat(400), null]]) assert.equal(reviewTime(hash), expected);
});

test('portrait composition fills most of the height and stays inside the screen', () => {
  const r = buildRose(17, false);
  for (const [width, height] of [[1000,800], [375,812], [320,740], [800,375]]) {
    const box = layout(width,height), bounds = [Infinity,Infinity,-Infinity,-Infinity];
    for (const s of r.strokes) for (let k = 0; k < s.points.length; k += 2) {
      const x = box.x+s.points[k]*box.width, y = box.y+s.points[k+1]*box.height;
      bounds[0] = Math.min(bounds[0],x); bounds[1] = Math.min(bounds[1],y);
      bounds[2] = Math.max(bounds[2],x); bounds[3] = Math.max(bounds[3],y);
    }
    assert.ok(bounds[0] > 10 && bounds[2] < width-10 && bounds[1] > 10 && bounds[3] < height-10);
    assert.ok((bounds[3]-bounds[1])/height > 0.65 && (bounds[3]-bounds[1])/height < 0.9);
    assert.ok(Math.abs(box.width/box.height - 2/3) < 1e-12);
  }
});

function context({ throwing = false, record = false } = {}) {
  let hash = createHash('sha256'), calls = 0;
  const bounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  const ctx = {};
  for (const name of ['setTransform', 'fillRect', 'beginPath', 'moveTo', 'lineTo', 'stroke', 'arc', 'fill', 'drawImage', 'fillText']) {
    ctx[name] = (...args) => {
      if (throwing) throw Error('canvas lost');
      assert.ok(args.slice(name === 'drawImage' || name === 'fillText' ? 1 : 0).every(Number.isFinite), `${name}: finite coordinates`);
      if (name === 'drawImage') args[0] = 'cached-layer';
      if (name === 'moveTo' || name === 'lineTo') {
        bounds.minX = Math.min(bounds.minX, args[0]); bounds.maxX = Math.max(bounds.maxX, args[0]);
        bounds.minY = Math.min(bounds.minY, args[1]); bounds.maxY = Math.max(bounds.maxY, args[1]);
      }
      if (record) hash.update(JSON.stringify([name, ...args,
        name === 'fillRect' || name === 'fillText' ? ctx.fillStyle : name === 'stroke' ? ctx.strokeStyle : null,
        name === 'fillRect' || name === 'stroke' ? ctx.globalAlpha : null,
        name === 'stroke' ? ctx.lineWidth : null]));
      calls++;
    };
  }
  ctx.measureText = text => ({ width: text.length * 12 });
  ctx.createLinearGradient = () => ({ addColorStop() {} });
  ctx.digest = () => { const result = hash.digest('hex'); hash = createHash('sha256'); return result; };
  ctx.calls = () => calls;
  ctx.bounds = () => bounds;
  return ctx;
}

test('all review frames are deterministic without replaying earlier simulation steps', () => {
  const f = buildRose(137508, false), a = context({ record: true }), b = context({ record: true });
  const drawA = createRenderer(a, f, fakeCanvas), drawB = createRenderer(b, buildRose(137508, false), fakeCanvas);
  for (const t of [0, 0.2, 0.8, 1.9, 2.6, 4.2, 5.8, 7.5, 9.6, 10.3, 10.8, 11.2, 11.8, 12.6, 13.6, 14, 14.8]) {
    drawA(t, 800, 600, 2); drawB(t, 800, 600, 2);
    assert.equal(a.digest(), b.digest(), `frame ${t}`);
  }
  drawA(2.6, 800, 600, 2); const direct = a.digest();
  drawA(4.0, 800, 600, 2); a.digest();
  drawA(2.6, 800, 600, 2); assert.equal(a.digest(), direct);
  assert.equal(a.globalCompositeOperation, 'source-over');
});

class Element {
  constructor() { this.attrs = new Map(); this.inert = false; this.focused = false; this.removed = false; }
  setAttribute(name, value) { this.attrs.set(name, value); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  removeAttribute(name) { this.attrs.delete(name); }
  remove() { this.removed = true; }
  focus() { this.focused = true; }
}

function harness(options = {}) {
  const listeners = new Map(), windowListeners = new Map(), timers = new Map(), frames = new Map();
  const classes = new Set(), card = new Element(), username = new Element(), button = new Element();
  button.setAttribute('tabindex', '3');
  card.querySelectorAll = () => [username, button]; card.contains = el => el === username;
  const ctx = context({ throwing: options.throwRender, record: options.record });
  const canvas = new Element(), offscreens = [];
  let created = false;
  canvas.getContext = () => { if (options.throwCanvas) throw Error('no canvas'); return options.noCanvas ? null : ctx; };
  const classList = { add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)) };
  const media = { matches: !!options.reduced, addEventListener: (n, fn) => windowListeners.set('media', fn),
    removeEventListener: () => windowListeners.delete('media') };
  const doc = { documentElement: { classList }, readyState: options.loading ? 'loading' : 'complete',
    body: { appendChild: () => {} }, querySelector: () => card, getElementById: () => username,
    createElement: () => {
      if (!created) { created = true; return canvas; }
      const layer = new Element(); layer.ctx = context(); layer.getContext = () => options.noOffscreen ? null : layer.ctx;
      offscreens.push(layer); return layer;
    }, addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: name => listeners.delete(name) };
  let nextID = 0;
  const win = { document: doc, innerWidth: options.width || 800, innerHeight: options.height || 600, devicePixelRatio: 3,
    navigator: { hardwareConcurrency: options.concurrency || 8 },
    location: { hash: options.hash || '' }, matchMedia: () => media,
    setTimeout: (fn, ms) => { const id = ++nextID; timers.set(id, { fn, ms }); return id; },
    clearTimeout: id => timers.delete(id),
    requestAnimationFrame: fn => { const id = ++nextID; frames.set(id, fn); return id; },
    cancelAnimationFrame: id => frames.delete(id),
    addEventListener: (name, fn) => windowListeners.set(name, fn),
    removeEventListener: name => windowListeners.delete(name) };
  if (options.noRAF) delete win.requestAnimationFrame;
  if (options.noCancelRAF) delete win.cancelAnimationFrame;
  if (options.throwRAF) win.requestAnimationFrame = () => { throw Error('scheduler failed'); };
  function tick(timestamp) {
    const pending = [...frames.values()]; frames.clear();
    for (const fn of pending) fn(timestamp);
  }
  function timeout(ms) {
    for (const [id, timer] of [...timers]) if (timer.ms === ms) { timers.delete(id); timer.fn(); }
  }
  return { win, doc, card, username, button, canvas, classes, timers, frames, listeners, windowListeners, ctx, offscreens, tick, timeout };
}

function assertFinished(h) {
  assert.equal(h.classes.size, 0);
  assert.equal(h.card.inert, false);
  assert.equal(h.card.getAttribute('aria-hidden'), null);
  assert.equal(h.username.getAttribute('tabindex'), null);
  assert.equal(h.button.getAttribute('tabindex'), '3');
  assert.equal(h.frames.size, 0); assert.equal(h.timers.size, 0);
  assert.equal(h.listeners.size, 0); assert.equal(h.windowListeners.size, 0);
  assert.equal(h.username.focused, true);
}

test('classic head script hides before body parsing and starts on DOMContentLoaded', () => {
  const h = harness({ loading: true });
  vm.runInNewContext(artSource, { window: h.win });
  vm.runInNewContext(source, { window: h.win });
  assert.ok(h.win.IgrisIntro.buildRose);
  assert.ok(h.classes.has('intro-active'));
  assert.equal(h.frames.size, 0);
  h.listeners.get('DOMContentLoaded')();
  assert.equal(h.card.inert, true);
  h.timeout(15200);
  assertFinished(h);
});

test('natural playback reveals from 14.0s, removes canvas at 14.8s, focuses and stops completely', () => {
  const h = harness(); startIntro(h.win);
  assert.equal(h.card.inert, true); assert.equal(h.card.getAttribute('aria-hidden'), 'true');
  assert.equal(h.username.getAttribute('tabindex'), '-1'); assert.equal(h.username.focused, false);
  assert.equal(h.canvas.width, 1600); assert.equal(h.canvas.height, 1200); // DPR capped at two.
  h.tick(100); h.tick(1800); h.tick(3500); h.tick(14099);
  assert.ok(!h.classes.has('intro-revealing'));
  h.tick(14100);
  assert.ok(h.classes.has('intro-revealing')); assert.equal(h.frames.size, 1);
  h.tick(14900);
  assert.ok(h.canvas.removed); assertFinished(h);
  const calls = h.ctx.calls(); h.tick(8000); assert.equal(h.ctx.calls(), calls);
});

test('click, pointer, touch and the first key are consumed and crossfade in 200ms', () => {
  for (const name of ['click', 'pointerdown', 'touchstart', 'keydown']) {
    const h = harness(); startIntro(h.win); h.tick(0); h.tick(350);
    let prevented = false, stopped = false;
    h.listeners.get(name)({ key: 'a', preventDefault: () => { prevented = true; }, stopImmediatePropagation: () => { stopped = true; } });
    assert.ok(prevented && stopped);
    assert.ok(h.classes.has('intro-fast')); assert.equal(h.frames.size, 0);
    assert.equal(h.username.focused, false);
    h.timeout(200); assert.ok(h.canvas.removed); assertFinished(h);
  }
});

test('skipping during the natural reveal shortens it once without restarting the fade', () => {
  const h = harness(); startIntro(h.win); h.tick(0); h.tick(14000);
  const event = { preventDefault() {}, stopImmediatePropagation() {} };
  h.listeners.get('keydown')(event);
  assert.ok(h.classes.has('intro-fast'));
  assert.equal(h.frames.size, 0);
  assert.equal([...h.timers.values()].filter(t => t.ms === 800).length, 0);
  const ids = [...h.timers.keys()];
  h.listeners.get('keydown')(event);
  assert.deepEqual([...h.timers.keys()], ids);
  h.timeout(200); assertFinished(h);
});

test('reduced motion, missing APIs, setup/drawing/scheduling failures immediately restore login', () => {
  for (const options of [{ reduced: true }, { noRAF: true }, { noCancelRAF: true }, { noCanvas: true },
    { throwCanvas: true }, { throwRAF: true }, { throwRender: true }, { noOffscreen: true }]) {
    const h = harness(options); startIntro(h.win);
    if (options.throwRender) h.tick(0);
    assertFinished(h);
  }
});

test('15.2-second watchdog and reveal timer work even when RAF stalls', () => {
  const h = harness(); startIntro(h.win); h.timeout(15200); assertFinished(h);
  const fade = harness(); startIntro(fade.win); fade.tick(0); fade.tick(14000);
  fade.timeout(800); assertFinished(fade);
});

test('motion preference changes also release the card and stop the loop', () => {
  const h = harness(); startIntro(h.win);
  h.windowListeners.get('media')({ matches: true }); assertFinished(h);
});

test('resize rescales without restarting and review stays frozen beyond the watchdog', () => {
  const h = harness({ hash: '#t=2.6', record: true }); startIntro(h.win);
  const firstFrame = h.ctx.digest();
  assert.equal(h.frames.size, 0); assert.equal(h.timers.size, 0);
  assert.equal(h.card.inert, true); assert.equal(h.username.focused, false);
  h.timeout(15200); assert.ok(!h.classes.has('intro-revealing'));
  h.win.innerWidth = 320; h.win.innerHeight = 740; h.win.devicePixelRatio = 1;
  h.windowListeners.get('resize')(); h.ctx.digest();
  assert.equal(h.canvas.width, 320); assert.equal(h.canvas.height, 740);
  h.win.innerWidth = 800; h.win.innerHeight = 600; h.win.devicePixelRatio = 3;
  h.windowListeners.get('resize')(); assert.equal(h.ctx.digest(), firstFrame);
  const live = harness({ hash: '#t=invalid' }); startIntro(live.win);
  assert.equal(live.frames.size, 1); live.timeout(15200); assertFinished(live);
});

test('review hashes seek directly in either direction and clearing the hash restores login', () => {
  const h = harness({ hash: '#t=2.6', record: true }); startIntro(h.win);
  const original = h.ctx.digest();
  h.win.location.hash = '#t=4.2'; h.windowListeners.get('hashchange')();
  assert.notEqual(h.ctx.digest(), original);
  assert.equal(h.frames.size, 0); assert.equal(h.timers.size, 0);
  assert.equal(h.card.inert, true); assert.ok(!h.classes.has('intro-revealing'));
  h.win.location.hash = '#t=2.6'; h.windowListeners.get('hashchange')();
  assert.equal(h.ctx.digest(), original);
  h.win.location.hash = ''; h.windowListeners.get('hashchange')();
  assertFinished(h);
});

test('missing artwork fails open in the classic script', () => {
  const h = harness();
  vm.runInNewContext(source, { window: h.win });
  assertFinished(h);
});

test('completed strokes are cached once; holding and burning never repaint the cached drawing', () => {
  const h = harness(); startIntro(h.win); h.tick(0); h.tick(8200);
  assert.equal(h.offscreens.length, 3);
  const counts = h.offscreens.map(c => c.ctx.calls());
  assert.ok(counts.every(n => n > 1000));
  const before = h.ctx.calls(); h.tick(8399);
  assert.ok(h.ctx.calls()-before < 1000, 'hold only blits the drawing and fades the pens');
  h.tick(11100); h.tick(12100);
  assert.deepEqual(h.offscreens.map(c => c.ctx.calls()), counts);
  h.win.innerWidth = 1000; h.windowListeners.get('resize')(); h.tick(12200);
  assert.ok(h.offscreens.every((c,i) => c.ctx.calls() > counts[i]));
  h.timeout(15200); assertFinished(h);
});

test('incremental playback and a direct still composite exactly the same cached stroke commands', () => {
  const r = buildRose(137508, true), layersA = [], layersB = [];
  const factory = layers => () => { const ctx = context({ record: true }); layers.push(ctx); return { getContext: () => ctx }; };
  const a = createRenderer(context(),r,factory(layersA)), b = createRenderer(context(),r,factory(layersB));
  for (const t of [0,0.8,1.5,2.7,3.6,4.4,5.6,7,9,10.3]) a(t,375,812,2);
  b(10.3,375,812,2);
  assert.deepEqual(layersA.map(c => c.digest()),layersB.map(c => c.digest()));
});

test('all bright pen cores follow their progressive stroke prefix and continuous travel', () => {
  const r = buildRose(42,true), ctx = context(), tips = [];
  ctx.arc = (x,y,radius) => { if (radius === 1.1) tips.push([x,y]); };
  const draw = createRenderer(ctx,r,fakeCanvas), box = layout(600,900), position = new Float64Array(5);
  for (const t of [0,0.25,0.5,1.7,3.3,5.4,6.2]) {
    tips.length = 0; draw(t,600,900,1);
    assert.equal(tips.length,r.schedule.penCount);
    for (let pen = 0; pen < tips.length; pen++) {
      samplePen(r,pen,t,position);
      assert.ok(Math.abs(tips[pen][0]-(box.x+position[0]*box.width)) < 1e-6);
      assert.ok(Math.abs(tips[pen][1]-(box.y+position[1]*box.height)) < 1e-6);
    }
  }
});

test('burn uses the prescribed colors and removes the entire rose before the word stands alone', () => {
  const ctx = context(), colors = new Set(), blits = [];
  ctx.stroke = () => colors.add(ctx.strokeStyle);
  ctx.drawImage = () => blits.push(ctx.globalAlpha);
  const draw = createRenderer(ctx,buildRose(31,true),fakeCanvas);
  for (const t of [9.35,9.6,10,10.8,11.3]) draw(t,375,812,1);
  for (const color of ['#fff0d9','#ffbf55','#ff6a2a','#7a1c0c','#8a8fa6']) assert.ok(colors.has(color),color);
  colors.clear(); blits.length = 0; draw(13.6,375,812,1);
  assert.equal(blits.length,1, 'only the paper layer survives, with no stem or leaf layer');
  assert.ok(colors.has('#d9dcec'));
  for (const color of ['#fff0d9','#ffbf55','#ff6a2a','#7a1c0c']) assert.ok(!colors.has(color));
});


test('every pen holds at the heart then fans out continuously before the sketch midpoint', () => {
  for (const low of [false,true]) {
    const r = buildRose(42,low), out = new Float64Array(5), positions = [];
    for (const pen of r.schedule.pens) {
      for (const t of [0,0.1,0.25,0.3]) {
        samplePen(r,pen.id,t,out);
        assert.equal(out[0],307/600); assert.equal(out[1],270/900);
        assert.ok(out[2] > 0);
      }
      const first = r.strokes[pen.strokeIds[0]];
      assert.equal(first.moveStart,0.3);
      samplePen(r,pen.id,(first.moveStart+first.start)/2,out);
      assert.ok(Math.hypot(out[0]-pen.initialX,out[1]-pen.initialY) > 0);
      samplePen(r,pen.id,3.15,out); positions.push([out[0],out[1]]);
      assert.ok(pen.strokeIds.every(id => ['gesture','contour'].includes(r.strokes[id].kind)));
    }
    assert.equal(new Set(positions.map(p => p.join(','))).size,r.schedule.penCount);
    assert.ok(Math.max(...positions.map(p => p[1]))-Math.min(...positions.map(p => p[1])) > 0.4);
    assert.equal(Math.max(...r.strokes.filter(s => s.pen >= 0).map(s => s.end)),6);
  }
});

test('all shading, veins, accents and dew grow behind the top-to-bottom sweep', () => {
  for (const low of [false,true]) for (const seed of [42,137508]) {
    const r = buildRose(seed,low), schedule = r.schedule;
    const marks = r.strokes.filter(s => ['shading','details'].includes(s.kind)).sort((a,b) => a.topY-b.topY);
    for (const [i,s] of marks.entries()) {
      const reached = 6+(s.topY-schedule.sweepTop)/(schedule.sweepBottom-schedule.sweepTop)*schedule.sweepDuration;
      assert.equal(s.pen,-1); assert.ok(s.start >= 6 && s.end <= 8.2);
      assert.ok(s.start >= reached && s.start <= reached+0.05);
      assert.ok(s.end-s.start >= 0.12-1e-12 && s.end-s.start <= 0.25+1e-12);
      if (i) assert.ok(s.start >= marks[i-1].start-0.05);
    }
  }
});

test('gravity drop begins above the viewport and impacts inside the upper head at ignition', () => {
  const r = buildRose(42,false), out = new Float64Array(4);
  for (const [width,height] of [[1440,900],[375,812],[800,375]]) {
    const paper = layout(width,height), ys = [];
    for (const t of [8.4,8.6,8.8,9,9.2,9.3]) { sampleDrop(t,paper,out); ys.push(out[1]); }
    assert.ok(ys[0] < 0);
    assert.ok(ys[2]-ys[1] > ys[1]-ys[0]); assert.ok(ys[3]-ys[2] > ys[2]-ys[1]);
    const x = (out[0]-paper.x)/paper.width, y = (out[1]-paper.y)/paper.height;
    assert.ok(x >= r.headBounds[0] && x <= r.headBounds[2]);
    assert.ok(y >= r.headBounds[1] && y < (r.headBounds[1]+r.headBounds[3])/2);
    assert.ok(Math.abs(x-r.ignition[0]) < 1e-12 && Math.abs(y-r.ignition[1]) < 1e-12);
    assert.equal(burnTime(x,y),9.3); assert.equal(out[2],0);
  }
});

test('ash writes bounded targets left to right and settles exactly with reproducible curved wind', () => {
  for (const low of [false,true]) for (const [width,height] of [[1440,900],[375,812]]) {
    const r = buildRose(137508,low), paper = layout(width,height);
    const field = buildParticles(r,width,height,paper), again = buildParticles(r,width,height,paper);
    assert.deepEqual(field,again);
    assert.deepEqual(field.box,wordBox(width,height));
    assert.equal(field.box.width,Math.min(width*0.34,320));
    assert.ok(Math.abs(field.box.y+field.box.height/2-height*0.36) < 1e-12);
    if (!low) assert.ok(field.wordCount >= 1200 && field.wordCount <= 2000);
    const out = new Float64Array(4), other = new Float64Array(4), word = field.particles.filter(p => p.word);
    const means = Array.from({length:5},(_,i) => {
      const letter = word.filter(p => p.letter === i);
      return letter.reduce((sum,p) => sum+p.arrival,0)/letter.length;
    });
    for (let i = 1; i < means.length; i++) assert.ok(means[i] > means[i-1]);
    word.sort((a,b) => a.targetX-b.targetX);
    for (const [i,p] of word.entries()) {
      assert.ok(p.targetX >= field.box.x && p.targetX <= field.box.x+field.box.width);
      assert.ok(p.targetY >= field.box.y && p.targetY <= field.box.y+field.box.height);
      assert.ok(p.arrival >= 11.4 && p.arrival <= 13.6);
      if (i) assert.ok(p.arrival >= word[i-1].arrival);
      assert.ok(p.release >= burnTime(p.sourceX,p.sourceY)+0.34-1e-12);
      for (const t of [p.arrival,13.6,14]) {
        sampleParticle(p,t,out);
        assert.ok(Math.hypot(out[0]-p.targetX,out[1]-p.targetY) <= 1.5);
      }
    }
    let curved = 0;
    field.particles.forEach((p,i) => {
      const mid = (p.release+p.arrival)/2;
      for (const t of [p.release,mid,p.arrival,14]) {
        sampleParticle(p,t,out); sampleParticle(again.particles[i],t,other);
        assert.deepEqual(out,other);
      }
      sampleParticle(p,mid,out);
      const cross = (out[0]-p.x)*(p.targetY-p.y)-(out[1]-p.y)*(p.targetX-p.x);
      if (Math.abs(cross)/Math.max(1,Math.hypot(p.targetX-p.x,p.targetY-p.y)) > 2) curved++;
      if (!p.word) { assert.ok(p.targetX > width); sampleParticle(p,13.6,out); assert.equal(out[2],0); }
    });
    assert.ok(curved > field.particles.length*0.85);
    assert.ok(!r.strokes.some(s => s.label === 'signature'));
  }
});

function inside(point, polygon) {
  let result = false;
  for (let i = 0,j = polygon.length-1; i < polygon.length; j = i++) {
    const a = polygon[i], b = polygon[j];
    if ((a[1] > point[1]) !== (b[1] > point[1]) && point[0] < (b[0]-a[0])*(point[1]-a[1])/(b[1]-a[1])+a[0]) result = !result;
  }
  return result;
}
function intersects(a,b,c,d) {
  const cross = (p,q,r) => (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]);
  if (Math.max(a[0],b[0])+1e-9 < Math.min(c[0],d[0]) || Math.max(c[0],d[0])+1e-9 < Math.min(a[0],b[0]) ||
      Math.max(a[1],b[1])+1e-9 < Math.min(c[1],d[1]) || Math.max(c[1],d[1])+1e-9 < Math.min(a[1],b[1])) return false;
  return cross(a,b,c)*cross(a,b,d) <= 1e-9 && cross(c,d,a)*cross(c,d,b) <= 1e-9;
}
function overlaps(a,b) {
  if (a.some(p => inside(p,b)) || b.some(p => inside(p,a))) return true;
  return a.some((p,i) => b.some((q,j) => intersects(p,a[(i+1)%a.length],q,b[(j+1)%b.length])));
}
test('outer petals overlap neighbouring regions and the upper back petal tucks into the front rim', () => {
  const r = buildRose(42,false), petals = r.petals;
  for (let i = 6; i < petals.length; i++) {
    assert.ok(petals.some((other,j) => i !== j && overlaps(petals[i],other)), `isolated petal ${i}`);
    // Both endpoints of an outer outline terminate on or within another petal.
    for (const point of [petals[i][0],petals[i][40]])
      assert.ok(petals.some((other,j) => i !== j && (inside(point,other) || other.some(p => Math.hypot(p[0]-point[0],p[1]-point[1]) < 1))), `floating edge ${i}`);
  }
  assert.ok(petals[8].slice(1,-1).some(p => inside(p,petals[7])), 'back petal has actual coverage, not just endpoint contact');
  const seen = new Set([0]);
  for (let pass = 0; pass < petals.length; pass++) for (let i = 0; i < petals.length; i++)
    if ([...seen].some(j => overlaps(petals[i],petals[j]))) seen.add(i);
  assert.equal(seen.size,petals.length, 'the whole head is one connected arrangement');
});


test('the exact impact frame ignites at the droplet position and the last marks burn out by 11.6', () => {
  const r = buildRose(42,true), ctx = context(), arcs = [], fills = [], colors = new Set();
  ctx.arc = (x,y,radius) => arcs.push([x,y,radius]);
  ctx.fill = () => fills.push(ctx.fillStyle);
  ctx.stroke = () => colors.add(ctx.strokeStyle);
  const draw = createRenderer(ctx,r,fakeCanvas), box = layout(600,900);
  draw(9.3,600,900,1);
  assert.ok(fills.includes('#fff0d9'));
  assert.ok(arcs.some(([x,y,radius]) => radius === 2.2 && Math.abs(x-(box.x+r.ignition[0]*box.width)) < 1e-9 && Math.abs(y-(box.y+r.ignition[1]*box.height)) < 1e-9));
  colors.clear(); draw(11.6,600,900,1);
  for (const color of ['#fff0d9','#ffbf55','#ff6a2a','#7a1c0c']) assert.ok(!colors.has(color));
});
