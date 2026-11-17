// Dependency-free export of the actual renderer's strokes, plus numeric geometry checks.
// Run: node tools/preview_rose.mjs [seconds=8.3] [low|desktop] [width=600] [height=900]
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const { buildRose, createRenderer } = createRequire(import.meta.url)('../igris/static/intro.js');
const { buildParticles, wordBox, TIMELINE } = createRequire(import.meta.url)('../igris/static/intro_art.js');
const { layout, phaseAt } = createRequire(import.meta.url)('../igris/static/intro.js');
const t = Number(process.argv[2] ?? 8.3);
const width = Number(process.argv[4] ?? 600), height = Number(process.argv[5] ?? 900);
assert.ok(Number.isFinite(t) && t >= 0);
assert.ok([width,height].every(n => Number.isFinite(n) && n > 0 && n <= 4096));
const rose = buildRose(137508, process.argv[3] === 'low');
const bounds = [1, 1, 0, 0], kinds = {};
for (const s of rose.strokes) {
  kinds[s.kind] = (kinds[s.kind] || 0) + 1;
  for (let k = 0; k < s.points.length; k += 2) {
    const x = s.points[k], y = s.points[k + 1];
    assert.ok(x >= 0 && x <= 1 && y >= 0 && y <= 1, s.label);
    bounds[0] = Math.min(bounds[0], x); bounds[1] = Math.min(bounds[1], y);
    bounds[2] = Math.max(bounds[2], x); bounds[3] = Math.max(bounds[3], y);
  }
}
const number = n => Number(n.toFixed(3));
function canvas() {
  const c = { marks: [], width: 0, height: 0 };
  let paths = [], current = [];
  const ctx = {
    globalAlpha: 1, lineWidth: 1, strokeStyle: '#d9dcec', fillStyle: '#02030a',
    setTransform() {},
    beginPath() { paths = []; current = []; },
    moveTo(x, y) { current = [[x, y]]; paths.push(current); },
    lineTo(x, y) { current.push([x, y]); },
    arc(x, y, r, a, b) {
      this.moveTo(x + Math.cos(a) * r, y + Math.sin(a) * r);
      for (let k = 1; k <= 12; k++) this.lineTo(x + Math.cos(a+(b-a)*k/12)*r, y + Math.sin(a+(b-a)*k/12)*r);
    },
    stroke() {
      for (const path of paths) if (path.length > 1) c.marks.push(
        `<polyline points="${path.map(p => p.map(number).join(',')).join(' ')}" stroke="${this.strokeStyle}" stroke-width="${number(this.lineWidth)}" opacity="${number(this.globalAlpha)}"/>`);
    },
    fill() {
      for (const path of paths) if (path.length > 1) c.marks.push(
        `<polygon points="${path.map(p => p.map(number).join(',')).join(' ')}" fill="${this.fillStyle}" stroke="none" opacity="${number(this.globalAlpha)}"/>`);
    },
    fillRect(x, y, w, h) {
      c.marks.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${this.fillStyle}" opacity="${this.globalAlpha}"/>`);
    },
    drawImage(layer) { c.marks.push(`<g opacity="${number(this.globalAlpha)}">${layer.marks.join('')}\n</g>`); }
  };
  c.getContext = () => ctx;
  return c;
}
const output = canvas();
createRenderer(output.getContext(), rose, canvas)(t, width, height, 1);
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" fill="none" stroke-linecap="round">\n${output.marks.join('\n')}\n</svg>\n`;
const directory = new URL('../.claude/cache/', import.meta.url);
await mkdir(directory, { recursive: true });
await writeFile(new URL('rose-preview.svg', directory), svg);
console.log(JSON.stringify({ time: t, phase: phaseAt(t), viewport: [width,height], low: rose.lowQuality, strokes: rose.count,
  timeline: TIMELINE, wordBox: wordBox(width,height),
  wordParticles: buildParticles(rose,width,height,layout(width,height)).wordCount,
  pens: rose.schedule.penCount,
  // Speed in pixels per second on the original 600 by 900 paper (CSS scales with layout).
  stages: rose.schedule.stages.map(s => ({ kind: s.kind, end: number(s.end),
    drawingSpeed: number(s.speed*900),
    drawingRange: [Math.min(...s.speeds.filter(Boolean)),Math.max(...s.speeds)].map(v => number(v*900)),
    travelRange: [Math.min(...s.travelSpeeds.filter(Boolean)),Math.max(...s.travelSpeeds)].map(v => number(v*900)) })),
  totalNormalizedLength: number(rose.totalLength), bounds: bounds.map(number), kinds,
  output: '.claude/cache/rose-preview.svg' }, null, 2));
