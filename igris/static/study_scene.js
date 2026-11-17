import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';

const EPSILON = 1e-10;
const ROLES = { primary: '#5dffd0', secondary: '#9c9cff', accent: '#ffb66b', muted: '#9ba5bc', result: '#ff84b8' };
const names = { dot: 'Skaler çarpım', cross: 'Vektörel çarpım', angle: 'Açı', norm: 'Uzunluk', projection: 'İzdüşüm' };
const copy = value => JSON.parse(JSON.stringify(value));
const coordinates = value => Array.isArray(value) && value.length >= 2 && value.length <= 3 && value.every(Number.isFinite)
  ? [value[0], value[1], value[2] ?? 0] : null;
const sub = (a, b) => a.map((v, i) => v - b[i]);
const dot = (a, b) => a.reduce((sum, v, i) => sum + v * b[i], 0);
const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
const length = a => Math.hypot(...a);
const format = value => Array.isArray(value) ? `(${value.map(format).join('; ')})`
  : Number.isFinite(value) ? new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 4 }).format(Math.abs(value) < EPSILON ? 0 : value) : 'tanımsız';

function references(item) {
  return item.ids ?? item.vectors ?? item.between ?? [item.a ?? item.vector, item.b ?? item.onto].filter(Boolean);
}

// Pure calculations also cover degenerate vectors independently of WebGL.
export function computeReadouts(scene) {
  const labels = new Map((scene.objects ?? []).map(o => [o.id, o.label || o.id]));
  const vectors = new Map((scene.objects ?? []).filter(o => o.type === 'vector').map(o => {
    const from = coordinates(o.from), to = coordinates(o.to);
    return [o.id, from && to ? sub(to, from) : null];
  }));
  return (scene.readouts ?? []).map(item => {
    const op = item.op ?? item.type;
    const [a, b] = references(item).map(id => vectors.get(id));
    let value = null;
    if (a) {
      const na = length(a), nb = b && length(b);
      if (op === 'norm') value = na;
      if (b && op === 'dot') value = dot(a, b);
      if (b && op === 'cross') value = cross(a, b);
      if (b && op === 'angle' && na > EPSILON && nb > EPSILON) value = Math.acos(Math.max(-1, Math.min(1, dot(a.map(x => x / na), b.map(x => x / nb))))) * 180 / Math.PI;
      if (b && op === 'projection' && nb > EPSILON) {
        const unit = b.map(x => x / nb), scalar = dot(a, unit);
        value = unit.map(x => x * scalar);
      }
    }
    const [first, second] = references(item).map(id => labels.get(id) ?? id);
    const operands = op === 'norm' ? `|${first}|` : op === 'angle' || op === 'projection' ? `(${first}, ${second})` : `${first}${op === 'cross' ? '×' : '·'}${second}`;
    const label = names[op] ? `${names[op]} ${operands}` : item.label ?? 'Ölçüm';
    return { ...item, op, value, label, text: value === null ? 'tanımsız' : `${format(value)}${op === 'angle' ? '°' : ''}` };
  });
}

export function sceneFrame(scene) {
  const box = new THREE.Box3();
  const add = value => {
    if (Array.isArray(value) && value.length >= 2 && value.every(Number.isFinite)) {
      box.expandByPoint(new THREE.Vector3(value[0], value[1], value[2] ?? 0));
    }
  };
  for (const object of scene.objects ?? []) {
    if (object.type === 'vector' || object.type === 'line') { add(object.from); add(object.to); }
    if (object.type === 'point') add(object.at ?? object.position ?? object.to);
    if (object.type === 'angle' && object.at) add(object.at);
    if (object.type === 'plane') {
      const position = new THREE.Vector3(...(object.point ?? object.at ?? object.origin ?? [0, 0, 0]));
      const normal = new THREE.Vector3(...(object.normal ?? [0, 0, 1])).normalize();
      const rotation = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal);
      const half = (object.size ?? 4) / 2;
      for (const x of [-half, half]) for (const y of [-half, half]) {
        box.expandByPoint(new THREE.Vector3(x, y, 0).applyQuaternion(rotation).add(position));
      }
    }
  }
  // Reference planes and grid lines must not shrink the vectors in the view.
  const vectors = (scene.objects ?? []).filter(object => object.type === 'vector');
  if (vectors.length) {
    box.makeEmpty();
    vectors.forEach(object => { add(object.from); add(object.to); });
  }
  if (box.isEmpty()) box.setFromCenterAndSize(new THREE.Vector3(), new THREE.Vector3(1, 1, 1));
  const size = box.getSize(new THREE.Vector3());
  const scale = Math.max(size.x, size.y, size.z) || 1;
  return { center: box.getCenter(new THREE.Vector3()), scale, radius: Math.max(size.length() / 2, scale * .75),
    gridSize: scale * 1.6, floor: box.min.z, size };
}

export function cameraFitDistance(frame, fov, aspect) {
  const vertical = Math.tan(THREE.MathUtils.degToRad(fov) / 2);
  const horizontal = vertical * aspect;
  const direction = new THREE.Vector3(1.3, -1.6, 1.2).normalize();
  const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 0, 1), direction).normalize();
  const up = new THREE.Vector3().crossVectors(direction, right).normalize();
  // Fit the projected bounds to 60% of the limiting canvas dimension.
  let distance = frame.scale;
  for (const x of [-.5, .5]) for (const y of [-.5, .5]) for (const z of [-.5, .5]) {
    const point = new THREE.Vector3(frame.size.x * x, frame.size.y * y, frame.size.z * z);
    distance = Math.max(distance, point.dot(direction) + Math.max(
      Math.abs(point.dot(right)) / (.6 * horizontal), Math.abs(point.dot(up)) / (.6 * vertical)));
  }
  return distance;
}

export function createScene(container) {
  const world = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(43, 1, .01, 10000);
  camera.position.set(7, 6, 9);
  camera.up.set(0, 0, 1);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.domElement.tabIndex = 0;
  renderer.domElement.setAttribute('aria-label', 'Üç boyutlu sahne. Nesne düğmeleri ve koordinat alanlarıyla da düzenleyebilirsin.');
  container.append(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.autoRotate = false;
  controls.enableDamping = false;
  controls.target.set(1, 1, 0);
  let frame = sceneFrame({ objects: [] }), grid = null, axes = null;
  let content = new THREE.Group();
  world.add(content);
  let data = { objects: [], readouts: [] }, selected = null, selectCallback = () => {}, changeCallback = () => {};
  let pickables = [], labels = [], drag = null, pointerStart = null;
  const raycaster = new THREE.Raycaster();
  raycaster.params.Line.threshold = .15;
  const pointer = new THREE.Vector2();
  const colour = object => ROLES[object.color ?? object.colour ?? object.role] ?? ROLES.primary;
  const vector = point => new THREE.Vector3(...(coordinates(point) ?? [0, 0, 0]));

  function fitCamera() {
    const distance = cameraFitDistance(frame, camera.fov, camera.aspect);
    controls.target.copy(frame.center);
    camera.position.copy(frame.center).add(new THREE.Vector3(1.3, -1.6, 1.2).normalize().multiplyScalar(distance));
    camera.near = distance / 1000;
    camera.far = distance * 100;
    controls.minDistance = frame.scale * .15;
    controls.maxDistance = distance * 20;
    camera.updateProjectionMatrix();
    controls.update();
  }

  function fitGrid() {
    for (const helper of [grid, axes]) {
      if (!helper) continue;
      world.remove(helper);
      helper.geometry.dispose();
      helper.material.dispose();
    }
    grid = new THREE.GridHelper(frame.gridSize, 16, '#3d706a', '#1c3738');
    grid.rotation.x = Math.PI / 2;
    grid.position.set(frame.center.x, frame.center.y, frame.floor);
    axes = new THREE.AxesHelper(frame.scale * .65);
    axes.position.copy(frame.center);
    world.add(grid, axes);
    raycaster.params.Line.threshold = frame.scale * .02;
  }

  function makeLabel(text, position, color, id = null) {
    const element = document.createElement('span');
    element.className = 'scene-label';
    element.textContent = text;
    element.style.color = color;
    container.append(element);
    labels.push({ element, position, id });
  }

  function render() {
    if (!container.clientWidth || !container.clientHeight) return;
    renderer.render(world, camera);
    labels.forEach(({ element, position }) => {
      const p = position.clone().project(camera);
      element.hidden = p.z < -1 || p.z > 1 || Math.abs(p.x) > 1 || Math.abs(p.y) > 1;
      element.style.left = `${(p.x + 1) * container.clientWidth / 2}px`;
      element.style.top = `${(1 - p.y) * container.clientHeight / 2}px`;
      const distance = camera.position.distanceTo(position);
      element.style.fontSize = `${THREE.MathUtils.clamp(frame.scale * container.clientHeight / Math.max(distance, frame.scale * .01) * .09, 12, 20)}px`;
    });
  }

  function discard() {
    content.traverse(object => {
      object.geometry?.dispose();
      if (Array.isArray(object.material)) object.material.forEach(m => m.dispose());
      else object.material?.dispose();
    });
    world.remove(content);
    content = new THREE.Group();
    world.add(content);
    labels.forEach(({ element }) => element.remove());
    labels = [];
    pickables = [];
  }

  function pointMesh(position, material, radius = frame.scale * .014) {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 14, 10), material);
    mesh.position.copy(position);
    return mesh;
  }

  function rebuild() {
    discard();
    [['x', [1, 0, 0]], ['y', [0, 1, 0]], ['z', [0, 0, 1]]].forEach(([text, p]) => makeLabel(text, vector(p).multiplyScalar(frame.scale * .68).add(frame.center), '#a0a5cf'));
    const byId = new Map(data.objects.map(o => [o.id, o]));
    data.objects.forEach(object => {
      const group = new THREE.Group();
      group.userData.id = object.id;
      const color = object.id === selected ? '#ffffff' : colour(object);
      const material = new THREE.MeshBasicMaterial({ color });
      let anchor = null;
      if (object.type === 'vector') {
        const from = vector(object.from), to = vector(object.to), direction = to.clone().sub(from), size = direction.length();
        if (size > EPSILON) group.add(new THREE.ArrowHelper(direction.normalize(), from, size, color, Math.min(frame.scale * .07, size * .3), Math.min(frame.scale * .04, size * .2)));
        const tip = pointMesh(to, material, frame.scale * .018);
        tip.userData.tip = true;
        group.add(tip);
        anchor = to.clone().add(new THREE.Vector3(0, 0, frame.scale * .04));
      } else if (object.type === 'point') {
        anchor = vector(object.at ?? object.position ?? object.to);
        group.add(pointMesh(anchor, material));
      } else if (object.type === 'line') {
        const from = vector(object.from), to = vector(object.to);
        group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([from, to]), new THREE.LineBasicMaterial({ color })));
        anchor = from.clone().add(to).multiplyScalar(.5);
      } else if (object.type === 'plane') {
        const size = Number.isFinite(object.size) && object.size > 0 ? object.size : 4;
        const plane = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .18, side: THREE.DoubleSide, depthWrite: false }));
        const normal = vector(object.normal ?? [0, 0, 1]);
        if (normal.length() > EPSILON) plane.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal.normalize());
        plane.position.copy(vector(object.point ?? object.at ?? object.origin));
        group.add(plane);
        anchor = plane.position.clone();
      } else if (object.type === 'angle') {
        const [first, second] = references(object).map(id => byId.get(id));
        if (first?.type === 'vector' && second?.type === 'vector') {
          const a = vector(first.to).sub(vector(first.from)), b = vector(second.to).sub(vector(second.from));
          const center = vector(object.at ?? first.from);
          if (a.length() > EPSILON && b.length() > EPSILON) {
            a.normalize(); b.normalize();
            const angle = Math.acos(THREE.MathUtils.clamp(a.dot(b), -1, 1));
            let normal = new THREE.Vector3().crossVectors(a, b);
            if (normal.length() < EPSILON) normal.crossVectors(a, Math.abs(a.z) < .9 ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(0, 1, 0));
            normal.normalize();
            const points = Array.from({ length: 49 }, (_, i) => a.clone().applyAxisAngle(normal, angle * i / 48).multiplyScalar(frame.scale * .12).add(center));
            group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color })));
            anchor = points[24];
          }
        }
      }
      if (anchor) makeLabel(object.label || object.id, anchor, color, object.id);
      if (!group.children.some(child => child.material === material)) material.dispose();
      group.traverse(child => { child.userData.id = object.id; });
      pickables.push(group);
      content.add(group);
    });
    render();
  }

  function select(id) {
    const object = data.objects.find(item => item.id === id);
    selected = object?.id ?? null;
    rebuild();
    selectCallback(object ? { id: object.id, label: object.label || object.id } : null);
  }

  function aim(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
  }

  renderer.domElement.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    aim(event);
    const hit = raycaster.intersectObjects(pickables, true)[0];
    pointerStart = { x: event.clientX, y: event.clientY, id: hit?.object.userData.id ?? null };
    const object = data.objects.find(item => item.id === hit?.object.userData.id);
    if (object?.type !== 'vector' || !object.draggable || !hit.object.userData.tip) return;
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(camera.getWorldDirection(new THREE.Vector3()), vector(object.to));
    const intersection = raycaster.ray.intersectPlane(plane, new THREE.Vector3());
    if (!intersection) return;
    drag = { id: object.id, plane, offset: vector(object.to).sub(intersection), pointer: event.pointerId };
    controls.enabled = false;
    renderer.domElement.setPointerCapture(event.pointerId);
    event.stopImmediatePropagation();
    select(object.id);
  }, true);

  renderer.domElement.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    aim(event);
    const intersection = raycaster.ray.intersectPlane(drag.plane, new THREE.Vector3());
    if (!intersection) return;
    const point = intersection.add(drag.offset).toArray().map(v => Math.round(v * 2) / 2);
    if (!point.every(Number.isFinite)) return;
    const object = data.objects.find(item => item.id === drag.id);
    object.to = point;
    rebuild();
    changeCallback(copy(data));
  });

  function release(event) {
    if (drag && event.pointerId !== drag.pointer) return;
    const moved = pointerStart && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 5;
    if (!drag && !moved && event.type === 'pointerup') select(pointerStart?.id ?? null);
    if (renderer.domElement.hasPointerCapture(event.pointerId)) renderer.domElement.releasePointerCapture(event.pointerId);
    drag = null;
    pointerStart = null;
    controls.enabled = true;
  }
  renderer.domElement.addEventListener('pointerup', release);
  renderer.domElement.addEventListener('pointercancel', release);
  renderer.domElement.addEventListener('lostpointercapture', () => { drag = null; controls.enabled = true; });
  container.addEventListener('study-select', event => select(event.detail));
  controls.addEventListener('change', render);
  const resize = new ResizeObserver(() => {
    const width = container.clientWidth, height = container.clientHeight;
    if (!width || !height) return;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    fitCamera();
    render();
  });
  resize.observe(container);
  controls.update();

  return {
    show(scene) {
      if (!scene || !Array.isArray(scene.objects) || scene.objects.length > 40) throw new Error('Sahne verisi geçersiz.');
      const ids = new Set();
      for (const object of scene.objects) {
        if (!object.id || ids.has(object.id) || !['vector', 'point', 'line', 'plane', 'angle'].includes(object.type)) throw new Error('Sahnedeki nesne geçersiz.');
        ids.add(object.id);
        if (['vector', 'line'].includes(object.type) && (!coordinates(object.from) || !coordinates(object.to))) throw new Error('Nesne koordinatları geçersiz.');
      }
      data = copy(scene);
      data.readouts ??= [];
      frame = sceneFrame(data);
      fitGrid();
      camera.aspect = (container.clientWidth || 1) / (container.clientHeight || 1);
      fitCamera();
      if (!ids.has(selected)) selected = null;
      rebuild();
    },
    onSelect(callback) { selectCallback = callback; },
    onChange(callback) { changeCallback = callback; },
    readouts() { return computeReadouts(data); },
  };
}
