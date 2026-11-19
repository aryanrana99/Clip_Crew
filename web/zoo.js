/* ClipCrew — every agent is a stylised 3D crew member standing on their own spot.
   Spots sit around a path in working order; the Director (the Manager) stands on the podium in the middle.
   Sand orbs travel along the path: each one is a clip moving through production. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ═══════════════════════════ agents & state ═══════════════════════════ */
const ORDER = ['calendar', 'scout', 'recorder', 'chat', 'detector', 'cutter', 'captioner', 'titler', 'editor'];
const TITLES = {
  manager: 'Manager', calendar: 'Calendar Analyst', scout: 'Live Scout', recorder: 'Stream Recorder', chat: 'Chat Monitor',
  detector: 'Hype Detector', cutter: 'Clip Cutter', captioner: 'Caption Writer', titler: 'Title Writer', editor: 'Video Editor',
};
const PERSONA = {
  manager: ['The Director', 'Keeps the whole crew moving. Headset on, clipboard always in hand.'],
  calendar: ['The Archivist', 'Remembers every stream and never goes anywhere without the big calendar.'],
  scout: ['The Lookout', 'Binoculars up — spots a stream the second it starts.'],
  recorder: ['The Camera Op', 'Camera on the shoulder, always rolling.'],
  chat: ['The Listener', 'Headphones on, reading every message chat sends.'],
  detector: ['The Hype Meter', 'Watches the needle and cheers when chat goes wild.'],
  cutter: ['The Cutter', 'Big scissors, clean cuts.'],
  captioner: ['The Scribe', 'Writes down every word, perfectly in time.'],
  titler: ['The Announcer', 'Shouts the title so nobody scrolls past.'],
  editor: ['The Finisher', 'Puts the whole video together on the laptop.'],
};
const STATUS = { idle: 'Idle', working: 'Working', waiting: 'Waiting', alert: 'Hype!', error: 'Knocked out', standby: 'Asleep' };
const RANK = { error: 5, alert: 4, working: 3, waiting: 2, idle: 1, standby: 0 };
const ENERGY = { standby: 0, idle: 0.12, waiting: 0.35, working: 1, alert: 2, error: 0 };
// floor spot under each crew member, by status: [color, opacity]
const SPOT = {
  standby: [0xDDDCD0, 0.4], idle: [0x7BA3CE, 0.35], waiting: [0xCACE00, 0.55],
  working: [0x025A5C, 0.55], alert: [0xE73F3C, 0.6], error: [0xE73F3C, 0.75],
};

const S = {
  roles: [], agents: new Map(), streamers: new Map(), jobs: new Map(), clips: [], logs: [],
  settings: {}, llm: {}, twitchApi: false, connected: false, panel: null, chat: {},
};
const roleMeta = (role) => S.roles.find((r) => r.role === role) || { role, title: TITLES[role] || role, desc: '', step: ORDER.indexOf(role) + 1, per_stream: ['recorder', 'chat', 'detector'].includes(role) };
const agentsOf = (role) => [...S.agents.values()].filter((a) => a.role === role);
const streamerName = (sid) => S.streamers.get(sid)?.name || String(sid || '').replace(/^(tw|yt)_/, '');
function roleState(role) {
  const meta = roleMeta(role), list = agentsOf(role);
  if (!list.length) return { status: meta.per_stream ? 'standby' : 'idle', task: meta.per_stream ? 'Asleep until a stream goes live' : 'Starting up…', count: 0 };
  const top = [...list].sort((a, b) => (RANK[b.status] ?? 0) - (RANK[a.status] ?? 0))[0];
  return { status: top.status, task: top.task, count: list.length };
}

/* ═══════════════════════════ renderer & scene ═══════════════════════════ */
const PAL = {
  // Masuga palette: Deep 1F3635 · Juice CACE00 · Cream FCFBF4 · Pink E9BAC3 · Heat E73F3C · Jade 025A5C · Sky 7BA3CE
  // (keys keep their old names so every prop and outfit picks the new colours up automatically)
  cream: 0xFCFBF4, creamDark: 0xECEBDF, sand: 0xCACE00, sandDark: 0xE9BAC3, sage: 0x025A5C, sageDark: 0x1F3635,
  sageLight: 0x7BA3CE, olive: 0x2C4746, walnut: 0x4E5D5C, ink: 0x1F3635, ground: 0xF3F2E9, heat: 0xE73F3C,
};
const host = $('#zoo');
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
} catch (err) {
  host.innerHTML = '<div class="nogl">The crew view needs WebGL, which this browser has turned off.</div>';
  throw err;
}
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
renderer.setSize(innerWidth, innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.outputColorSpace = THREE.SRGBColorSpace;
host.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
labelRenderer.domElement.className = 'labels';
host.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(PAL.cream);
scene.fog = new THREE.Fog(PAL.cream, 38, 80);
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
scene.environmentIntensity = 0.45;

const camera = new THREE.PerspectiveCamera(40, innerWidth / innerHeight, 0.1, 200);
const HOME = { pos: new THREE.Vector3(0, 17, 28), target: new THREE.Vector3(0, 1.4, 0) };
if (innerWidth < 760) HOME.pos.set(0, 26, 40);
camera.position.copy(HOME.pos);
const homeDist = HOME.pos.distanceTo(HOME.target);
let headerCompact = false;
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.copy(HOME.target);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = 1.38;
controls.minDistance = 5;
controls.maxDistance = 55;
controls.enablePan = false;

scene.add(new THREE.HemisphereLight(0xfffdf4, 0xE6E6DA, 0.9));
const sun = new THREE.DirectionalLight(0xfffaee, 1.7);
sun.position.set(10, 24, 14);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, { left: -20, right: 20, top: 20, bottom: -20, near: 1, far: 70 });
sun.shadow.bias = -0.0006;
sun.shadow.radius = 4;
scene.add(sun);
const fill = new THREE.DirectionalLight(0xdbe6f2, 0.5);
fill.position.set(-14, 8, -10);
scene.add(fill);

/* ═══════════════════════════ materials & shape helpers ═══════════════════════════ */
const matCache = new Map();
function mat(hex, o = {}) {
  const key = hex + JSON.stringify(o);
  if (!matCache.has(key)) matCache.set(key, new THREE.MeshStandardMaterial({ color: hex, roughness: 0.62, metalness: 0, ...o }));
  return matCache.get(key);
}
const M = {
  white: mat(0xFFFCF6, { roughness: 0.25 }),
  pupil: mat(0x17120e, { roughness: 0.2 }),
  mouth: mat(0x7A3E36, { roughness: 0.5 }),
  metal: mat(0xE9E4DA, { roughness: 0.25, metalness: 0.8 }),
};
const Sp = (r, w = 20, h = 16) => new THREE.SphereGeometry(r, w, h);
const Bx = (w, h, d) => new THREE.BoxGeometry(w, h, d);
const Cy = (a, b, h, s = 16) => new THREE.CylinderGeometry(a, b, h, s);
const Ca = (r, l) => new THREE.CapsuleGeometry(r, l, 6, 14);
const Co = (r, h, s = 12) => new THREE.ConeGeometry(r, h, s);
const To = (r, t, arc = Math.PI * 2, rs = 10, ts = 32) => new THREE.TorusGeometry(r, t, rs, ts, arc);
function mesh(geo, material, parent, x = 0, y = 0, z = 0) {
  const m = new THREE.Mesh(geo, material);
  m.position.set(x, y, z);
  m.castShadow = true;
  if (parent) parent.add(m);
  return m;
}
function grp(parent, x = 0, y = 0, z = 0) {
  const g = new THREE.Group();
  g.position.set(x, y, z);
  parent.add(g);
  return g;
}
const mix = (a, b, k) => a + (b - a) * k;
const clamp01 = (x) => Math.max(0, Math.min(1, x));
// arm pose: x = swing (negative = forward), out = raise away from the body (negative = across), el = elbow bend (negative = forward)
function arm(a, x, out, el) {
  a.sh.rotation.x = x;
  a.sh.rotation.z = a.s * out;
  a.el.rotation.x = el;
}

/* ═══════════════════════════ the character builder ═══════════════════════════ */
function hairFor(head, h, m) {
  const cap = mesh(Sp(0.53, 30, 22), m, head, 0, 0.18, -0.1);
  const at = (x, y, z, r, sx = 1, sy = 1, sz = 1) => mesh(Sp(r, 16, 12), m, head, x, y, z).scale.set(sx, sy, sz);
  switch (h.style) {
    case 'bun':
      at(0, 0.72, -0.2, 0.21);
      mesh(To(0.12, 0.03), mat(PAL.sand, { metalness: 0.4, roughness: 0.35 }), head, 0, 0.6, -0.16).rotation.x = 1.2;
      break;
    case 'sidepart':
      at(-0.14, 0.5, 0.2, 0.26, 1.35, 0.5, 1.05);
      break;
    case 'ponytail': {
      const t = grp(head, 0, 0.4, -0.5);
      mesh(Sp(0.08, 12, 10), mat(PAL.sand), t);
      const tail = mesh(Ca(0.13, 0.45), m, t, 0, -0.32, -0.08);
      tail.rotation.x = 0.3;
      break;
    }
    case 'buzz':
      cap.scale.set(0.985, 0.95, 0.985);
      break;
    case 'curly': {
      const n = h.volume || 1;
      for (let ring = 0; ring < 4; ring++) {
        const y = 0.18 + ring * 0.13, rr = Math.sqrt(Math.max(0, 0.53 ** 2 - (y - 0.18) ** 2)) * 1.02;
        const count = 11 - ring * 2;
        for (let i = 0; i < count; i++) {
          const a = (i / count) * Math.PI * 2 + ring;
          const z = Math.cos(a) * rr - 0.1;
          if (z > 0.3 && y < 0.42) continue; // keep the forehead clear
          at(Math.sin(a) * rr, y + 0.05, z, 0.15 * n);
        }
      }
      at(0, 0.7, -0.08, 0.2 * n);
      break;
    }
    case 'long':
      mesh(Ca(0.4, 0.6), m, head, 0, -0.32, -0.22).scale.set(1.15, 1, 0.55);
      for (const s of [-1, 1]) mesh(Ca(0.11, 0.55), m, head, s * 0.43, -0.22, 0.08);
      at(0.1, 0.46, 0.26, 0.22, 1.5, 0.42, 1);
      break;
    case 'bob':
      mesh(Sp(0.5, 26, 18), m, head, 0, -0.05, -0.12).scale.set(1.1, 0.88, 0.9);
      for (const s of [-1, 1]) mesh(Ca(0.14, 0.28), m, head, s * 0.44, -0.12, 0.12);
      at(-0.05, 0.45, 0.3, 0.24, 1.6, 0.38, 0.9);
      break;
    case 'puffs':
      for (const s of [-1, 1]) at(s * 0.37, 0.55, -0.08, 0.27);
      break;
    case 'quiff': {
      const q = mesh(Sp(0.24, 18, 14), m, head, 0, 0.6, 0.16);
      q.scale.set(0.95, 0.65, 1.35);
      q.rotation.x = -0.35;
      break;
    }
  }
}

function glassesFor(head, gl) {
  const m = mat(gl.color, { roughness: 0.3, metalness: gl.metal ? 0.7 : 0 });
  const t = gl.thin ? 0.011 : 0.019;
  for (const s of [-1, 1]) {
    const ring = gl.shape === 'round' ? mesh(To(0.13, t, Math.PI * 2, 8, 32), m, head, s * 0.17, 0.05, 0.5)
      : mesh(To(0.16, t, Math.PI * 2, 6, 4), m, head, s * 0.17, 0.05, 0.5);
    if (gl.shape !== 'round') { ring.rotation.z = Math.PI / 4; ring.scale.set(1.05, 0.8, 1); }
    const temple = mesh(Bx(0.014, 0.014, 0.42), m, head, s * 0.33, 0.07, 0.28);
    temple.rotation.y = s * 0.12;
  }
  mesh(Cy(0.011, 0.011, 0.08, 6), m, head, 0, 0.08, 0.52).rotation.z = Math.PI / 2;
}

function topFor(torso, o, topM) {
  const t = o.top, at = (geo, m, x, y, z) => mesh(geo, m, torso, x, y, z);
  const cream = mat(PAL.cream), accent = mat(t.accent ?? PAL.cream);
  switch (t.style) {
    case 'shirt': {
      for (const s of [-1, 1]) { const c = at(Bx(0.17, 0.05, 0.14), topM, s * 0.09, 0.9, 0.2); c.rotation.set(-0.3, 0, s * 0.5); }
      for (let i = 0; i < 3; i++) at(Sp(0.018, 8, 6), accent, 0, 0.74 - i * 0.16, 0.272);
      break;
    }
    case 'blazer':
      at(Bx(0.18, 0.42, 0.04), accent, 0, 0.64, 0.25);
      for (const s of [-1, 1]) { const l = at(Bx(0.08, 0.42, 0.03), topM, s * 0.12, 0.66, 0.27); l.rotation.z = s * 0.32; }
      at(Sp(0.025, 8, 6), mat(PAL.sand, { metalness: 0.5, roughness: 0.3 }), 0, 0.4, 0.275);
      break;
    case 'hoodie': {
      const hood = at(To(0.24, 0.09), topM, 0, 0.93, -0.06);
      hood.rotation.x = Math.PI / 2 - 0.3;
      for (const s of [-1, 1]) at(Cy(0.012, 0.012, 0.22, 6), cream, s * 0.07, 0.76, 0.27);
      at(Bx(0.32, 0.15, 0.03), topM, 0, 0.28, 0.27);
      break;
    }
    case 'turtle':
      at(Cy(0.15, 0.165, 0.22, 18), topM, 0, 0.98, 0);
      for (let i = 0; i < 4; i++) { const r = at(To(0.33, 0.012, Math.PI * 2, 6, 40), topM, 0, 0.05 + i * 0.03, 0); r.rotation.x = Math.PI / 2; r.scale.set(1, 0.8, 1); }
      break;
    case 'cardigan':
      at(Bx(0.16, 0.55, 0.04), accent, 0, 0.56, 0.25);
      for (let i = 0; i < 3; i++) at(Sp(0.022, 8, 6), mat(PAL.walnut), 0.1, 0.7 - i * 0.15, 0.27);
      for (const s of [-1, 1]) at(Bx(0.12, 0.1, 0.02), topM, s * 0.17, 0.28, 0.27);
      break;
    case 'vest':
      for (const s of [-1, 1]) {
        at(Bx(0.15, 0.55, 0.05), accent, s * 0.16, 0.5, 0.25);
        at(Bx(0.1, 0.09, 0.03), mat(t.accent2 ?? PAL.sandDark), s * 0.16, 0.42, 0.285);
      }
      break;
    case 'overall':
      at(Bx(0.36, 0.34, 0.05), accent, 0, 0.42, 0.255);
      for (const s of [-1, 1]) {
        const strap = at(Bx(0.06, 0.42, 0.05), accent, s * 0.13, 0.75, 0.18);
        strap.rotation.x = -0.2;
        at(Sp(0.025, 8, 6), mat(PAL.sand, { metalness: 0.5, roughness: 0.3 }), s * 0.13, 0.58, 0.29);
      }
      at(Bx(0.14, 0.1, 0.02), accent, 0, 0.45, 0.285);
      break;
    case 'apron':
      at(Bx(0.5, 0.95, 0.035), accent, 0, 0.14, 0.285);
      at(Bx(0.18, 0.1, 0.02), mat(t.accent2 ?? PAL.sandDark), 0.1, 0.05, 0.305);
      for (const s of [-1, 1]) { const st = at(Bx(0.04, 0.35, 0.03), accent, s * 0.14, 0.78, 0.2); st.rotation.z = -s * 0.25; }
      for (const s of [-1, 1]) { const c = at(Bx(0.15, 0.05, 0.12), topM, s * 0.08, 0.92, 0.2); c.rotation.set(-0.3, 0, s * 0.5); }
      break;
    case 'scarf': {
      const sc = at(To(0.17, 0.075), accent, 0, 0.93, 0.02);
      sc.rotation.x = Math.PI / 2;
      const tail = at(Ca(0.065, 0.36), accent, 0.12, 0.66, 0.27);
      tail.rotation.z = 0.12;
      break;
    }
    case 'denim':
      for (const s of [-1, 1]) {
        const c = at(Bx(0.2, 0.06, 0.16), topM, s * 0.1, 0.9, 0.2);
        c.rotation.set(-0.3, 0, s * 0.55);
        at(Bx(0.12, 0.1, 0.02), topM, s * 0.16, 0.64, 0.268);
        at(Sp(0.018, 8, 6), mat(PAL.sandDark, { metalness: 0.5, roughness: 0.3 }), s * 0.16, 0.66, 0.282);
      }
      at(Bx(0.16, 0.5, 0.035), accent, 0, 0.6, 0.255);
      break;
  }
}

function person(o) {
  const skin = mat(o.skin, { roughness: 0.48 });
  const hairM = mat(o.hair.color, { roughness: 0.72 });
  const topM = mat(o.top.color);
  const sleeveM = mat(o.top.sleeve ?? o.top.color);
  const pantsM = mat(o.pants);
  const shoeM = mat(o.shoes, { roughness: 0.45 });
  const w = o.width || 1;
  const g = new THREE.Group(), r = {};
  r.root = grp(g);
  r.legs = [-1, 1].map((s) => {
    const hip = grp(r.root, s * 0.16 * w, 1.0, 0);
    mesh(Ca(0.135, 0.5), pantsM, hip, 0, -0.38, 0);
    mesh(Sp(0.15, 16, 12), shoeM, hip, 0, -0.86, 0.07).scale.set(1.05, 0.62, 1.5);
    return hip;
  });
  r.torso = grp(r.root, 0, 1.0, 0);
  mesh(Ca(0.33 * w, 0.4), topM, r.torso, 0, 0.42, 0).scale.set(1, 1, 0.8);
  mesh(Sp(0.33 * w, 18, 12), o.top.style === 'overall' ? mat(o.top.accent) : pantsM, r.torso, 0, 0.04, 0).scale.set(1, 0.55, 0.82);
  mesh(Cy(0.1, 0.12, 0.22, 14), skin, r.torso, 0, 0.95, 0);
  r.arms = [-1, 1].map((s) => {
    const sh = grp(r.torso, s * 0.39 * w, 0.78, 0);
    mesh(Sp(0.125, 14, 10), sleeveM, sh);
    mesh(Ca(0.105, 0.26), sleeveM, sh, 0, -0.2, 0);
    const el = grp(sh, 0, -0.38, 0);
    mesh(Ca(0.092, 0.24), o.top.short ? skin : sleeveM, el, 0, -0.16, 0);
    if (o.top.short) mesh(Cy(0.11, 0.115, 0.06, 14), sleeveM, sh, 0, -0.34, 0);
    const hand = grp(el, 0, -0.36, 0);
    mesh(Sp(0.1, 14, 10), skin, hand).scale.set(0.9, 1.05, 0.8);
    return { sh, el, hand, s };
  });
  topFor(r.torso, o, topM);

  // the head: big, round and friendly
  r.head = grp(r.torso, 0, 1.47, 0);
  mesh(Sp(0.5, 36, 26), skin, r.head).scale.set(0.96, 1.04, 0.95);
  mesh(Sp(0.36, 24, 16), skin, r.head, 0, -0.2, 0.08).scale.set(1, 0.8, 1);
  for (const s of [-1, 1]) mesh(Sp(0.09, 12, 10), skin, r.head, s * 0.47, -0.02, 0).scale.set(0.5, 1, 0.8);
  mesh(Sp(0.065, 14, 10), skin, r.head, 0, -0.06, 0.48).scale.set(1, 0.85, 1);
  if (o.cheeks) for (const s of [-1, 1]) mesh(Sp(0.07, 12, 8), mat(o.cheeks, { roughness: 0.7 }), r.head, s * 0.27, -0.12, 0.38).scale.set(1, 0.6, 0.4);
  if (o.freckles) {
    for (const s of [-1, 1]) for (let i = 0; i < 4; i++) {
      mesh(Sp(0.011, 6, 4), mat(0xB0784F), r.head, s * (0.2 + (i % 2) * 0.06), -0.06 - Math.floor(i / 2) * 0.05, 0.43);
    }
  }
  r.eyes = [-1, 1].map((s) => {
    const e = grp(r.head, s * 0.17, 0.05, 0.41);
    mesh(Sp(0.115, 22, 16), M.white, e).scale.set(1, 1.08, 0.62);
    mesh(Sp(0.068, 16, 12), mat(o.eye ?? 0x5A3A24, { roughness: 0.3 }), e, 0, -0.005, 0.052).scale.set(1, 1, 0.45);
    mesh(Sp(0.038, 12, 10), M.pupil, e, 0, -0.005, 0.072).scale.set(1, 1, 0.4);
    mesh(Sp(0.017, 8, 6), M.white, e, 0.025, 0.03, 0.085);
    return e;
  });
  r.brows = [-1, 1].map((s) => {
    const b = mesh(Ca(0.022, 0.11), mat(o.brow ?? o.hair.color), r.head, s * 0.17, 0.22, 0.425);
    b.rotation.z = Math.PI / 2 - s * (o.browTilt ?? 0.12);
    return b;
  });
  const mz = o.beard ? 0.5 : 0.455;
  r.smile = mesh(To(0.1, 0.022, Math.PI, 8, 24), M.mouth, r.head, 0, -0.2, mz);
  r.smile.rotation.set(-0.25, 0, Math.PI);
  r.open = mesh(Sp(0.08, 14, 10), M.mouth, r.head, 0, -0.23, mz - 0.01);
  r.open.scale.set(1.1, 0.85, 0.4);
  r.open.visible = false;
  if (o.beard) {
    mesh(Sp(0.33, 22, 16), hairM, r.head, 0, -0.3, 0.14).scale.set(1.1, 0.72, 0.95);
    mesh(Ca(0.032, 0.14), hairM, r.head, 0, -0.135, 0.48).rotation.z = Math.PI / 2;
  }
  hairFor(r.head, o.hair, hairM);
  if (o.glasses) glassesFor(r.head, o.glasses);
  if (o.earrings) for (const s of [-1, 1]) mesh(Sp(0.035, 10, 8), mat(o.earrings, { metalness: 0.6, roughness: 0.3 }), r.head, s * 0.48, -0.13, 0.02);

  const c = { g, r, h: 3.25, blinkAt: 2 + Math.random() * 3 };
  return c;
}

/* ═══════════════════════════ props ═══════════════════════════ */
function headset(head, color, big = false) {
  const m = mat(color, { roughness: 0.4 });
  const band = mesh(To(0.56, big ? 0.04 : 0.026, Math.PI, 8, 30), m, head, 0, 0.08, -0.02);
  band.rotation.y = 0;
  for (const s of [-1, 1]) {
    const cup = mesh(big ? Sp(0.16, 16, 12) : Cy(0.1, 0.1, 0.08, 16), m, head, s * 0.52, 0.0, 0);
    if (big) cup.scale.set(0.55, 1, 1); else cup.rotation.z = Math.PI / 2;
    if (big) mesh(Cy(0.11, 0.11, 0.03, 16), mat(PAL.ink), head, s * 0.6, 0, 0).rotation.z = Math.PI / 2;
  }
  return m;
}

function directorKit(c) {
  headset(c.r.head, PAL.ink);
  const mic = mesh(Ca(0.012, 0.3), mat(PAL.ink), c.r.head, -0.42, -0.14, 0.24);
  mic.rotation.set(0.9, 0.6, 0);
  mesh(Sp(0.035, 10, 8), mat(PAL.sand), c.r.head, -0.26, -0.22, 0.42);
  const board = grp(c.r.torso, -0.08, 0.52, 0.44);
  board.rotation.set(-0.35, 0.25, 0);
  mesh(Bx(0.42, 0.54, 0.03), mat(PAL.walnut), board);
  mesh(Bx(0.36, 0.44, 0.012), mat(PAL.cream), board, 0, -0.02, 0.02);
  mesh(Bx(0.15, 0.05, 0.04), mat(PAL.sand, { metalness: 0.5, roughness: 0.3 }), board, 0, 0.26, 0.02);
  for (let i = 0; i < 4; i++) mesh(Bx(0.26 - (i % 2) * 0.08, 0.018, 0.01), mat(PAL.sage), board, -0.02, 0.12 - i * 0.08, 0.03);
  return {
    anim(t, e) {
      const k = clamp01(e);
      arm(c.r.arms[0], -0.45, -0.32, -1.5);
      const point = k * (0.55 + 0.45 * Math.sin(t * 0.8));
      arm(c.r.arms[1], mix(0.05, -1.25, point), mix(0.14, 0.25, point), mix(-0.2, -0.15, point));
      c.r.head.rotation.y = Math.sin(t * 0.45) * 0.25;
    },
  };
}

function archivistKit(c) {
  const board = grp(c.r.torso, 0, 0.58, 0.5);
  board.rotation.x = -0.25;
  mesh(Bx(0.72, 0.56, 0.04), mat(PAL.cream), board);
  mesh(Bx(0.72, 0.14, 0.05), mat(PAL.sage), board, 0, 0.21, 0.005);
  for (const s of [-1, 1]) mesh(To(0.03, 0.01), mat(PAL.sandDark, { metalness: 0.5 }), board, s * 0.18, 0.3, 0.02);
  const cells = [];
  for (let row = 0; row < 3; row++) for (let col = 0; col < 5; col++) {
    const cell = mesh(Bx(0.1, 0.08, 0.01), mat(PAL.sand), board, -0.24 + col * 0.12, 0.07 - row * 0.11, 0.025);
    cells.push(cell);
  }
  const mark = mesh(To(0.055, 0.012), mat(PAL.heat), board, 0, 0, 0.035);
  return {
    anim(t, e) {
      const k = clamp01(e);
      arm(c.r.arms[0], -0.5, -0.12, -1.35);
      arm(c.r.arms[1], -0.55 - 0.15 * k, -0.18, -1.35 + Math.sin(t * 6) * 0.25 * k);
      c.r.head.rotation.x = 0.15 * k + Math.sin(t * 0.7) * 0.03;
      c.r.head.rotation.y = Math.sin(t * 0.5) * 0.2;
      const pick = Math.floor(t * 0.8) % cells.length;
      mark.position.x = cells[pick].position.x;
      mark.position.y = cells[pick].position.y;
    },
  };
}

function lookoutKit(c) {
  const cap = grp(c.r.head, 0, 0.34, -0.04);
  mesh(new THREE.SphereGeometry(0.55, 26, 18, 0, Math.PI * 2, 0, Math.PI / 2), mat(PAL.sageDark), cap).scale.set(1, 0.62, 1);
  mesh(Cy(0.36, 0.36, 0.035, 24), mat(PAL.sageDark), cap, 0, 0.0, 0.42).scale.set(1, 1, 0.7);
  mesh(Sp(0.05, 8, 6), mat(PAL.sand), cap, 0, 0.34, 0);
  const bino = grp(c.r.torso, 0, 0.62, 0.32);
  for (const s of [-1, 1]) {
    mesh(Cy(0.07, 0.085, 0.24, 16), mat(PAL.ink), bino, s * 0.09, 0, 0).rotation.x = Math.PI / 2;
    mesh(Cy(0.09, 0.09, 0.03, 16), mat(PAL.sand), bino, s * 0.09, 0, 0.12).rotation.x = Math.PI / 2;
  }
  mesh(Bx(0.08, 0.05, 0.1), mat(PAL.ink), bino);
  const strap = mesh(To(0.3, 0.012, Math.PI, 6, 24), mat(PAL.walnut), c.r.torso, 0, 0.9, 0.1);
  strap.rotation.z = Math.PI;
  return {
    anim(t, e) {
      const k = clamp01(e * 1.2);
      bino.position.set(0, mix(0.62, 1.52, k), mix(0.32, 0.64, k));
      strap.visible = k < 0.5;
      arm(c.r.arms[0], mix(0.06, -2.25, k), mix(0.12, -0.35, k), mix(-0.2, -0.55, k));
      arm(c.r.arms[1], mix(0.06, -2.25, k), mix(0.12, -0.35, k), mix(-0.2, -0.55, k));
      c.r.torso.rotation.y = Math.sin(t * 0.7) * 0.5 * k;
      c.r.head.rotation.y = (1 - k) * Math.sin(t * 0.5) * 0.3;
    },
  };
}

function cameraKit(c) {
  const cam = grp(c.r.torso, 0.36, 1.08, 0.1);
  mesh(Bx(0.3, 0.3, 0.55), mat(PAL.ink, { roughness: 0.4 }), cam);
  mesh(Cy(0.11, 0.13, 0.2, 18), mat(PAL.olive), cam, 0, 0, 0.36).rotation.x = Math.PI / 2;
  mesh(Cy(0.09, 0.09, 0.02, 18), mat(0x2a3a3a, { roughness: 0.1, metalness: 0.6 }), cam, 0, 0, 0.47).rotation.x = Math.PI / 2;
  mesh(Bx(0.06, 0.06, 0.3), mat(PAL.sand), cam, 0, 0.19, -0.02);
  mesh(Bx(0.18, 0.12, 0.02), mat(PAL.sage), cam, -0.16, 0.05, 0.1).rotation.y = -0.4;
  const rec = mesh(Sp(0.03, 10, 8), new THREE.MeshBasicMaterial({ color: PAL.heat }), cam, 0.1, 0.12, 0.28);
  return {
    anim(t, e) {
      const k = clamp01(e);
      arm(c.r.arms[1], -1.15, -0.05, -1.75);
      arm(c.r.arms[0], mix(0.05, -1.1, k), mix(0.12, -0.42, k), mix(-0.2, -1.05, k));
      c.r.torso.rotation.y = Math.sin(t * 0.45) * 0.35 * k;
      c.r.head.rotation.y = 0.15 * k + Math.sin(t * 0.6) * 0.1;
      rec.visible = k > 0.5 && Math.sin(t * 5) > -0.2;
    },
  };
}

function listenerKit(c) {
  headset(c.r.head, PAL.sand, true);
  const phone = grp(c.r.torso, 0, 0.5, 0.44);
  phone.rotation.x = -0.75;
  mesh(Bx(0.18, 0.3, 0.025), mat(PAL.ink, { roughness: 0.3 }), phone);
  const screen = mesh(Bx(0.15, 0.26, 0.005), new THREE.MeshBasicMaterial({ color: PAL.sageLight }), phone, 0, 0, 0.015);
  // speech bubbles that float up while chat is flowing
  const bubbles = [];
  for (let i = 0; i < 5; i++) {
    const b = grp(c.g);
    mesh(Sp(0.17, 18, 12), mat(i % 2 ? PAL.sand : PAL.cream), b).scale.set(1.45, 1, 0.45);
    mesh(Co(0.06, 0.12, 8), mat(i % 2 ? PAL.sand : PAL.cream), b, -0.12, -0.15, 0).rotation.z = 0.5;
    for (let d = -1; d <= 1; d++) mesh(Sp(0.025, 8, 6), mat(PAL.sageDark), b, d * 0.08, 0, 0.08);
    b.visible = false;
    bubbles.push({ b, life: 1 });
  }
  let timer = 0;
  const o = {
    anim(t, e, dt) {
      const k = clamp01(e);
      arm(c.r.arms[0], -0.42, -0.3, -1.6);
      arm(c.r.arms[1], -0.42, -0.3, -1.6);
      c.r.head.rotation.x = 0.28 + Math.max(0, Math.sin(t * (3 + c.rate * 4))) * 0.07 * k;
      screen.material.color.setHex(k > 0.5 && Math.sin(t * 8) > 0.6 ? PAL.cream : PAL.sageLight);
      timer -= dt;
      if (k > 0.5 && timer <= 0) {
        timer = 1.6 / (0.6 + c.rate);
        const free = bubbles.find((x) => x.life >= 1);
        if (free) { free.life = 0; free.side = Math.random() < 0.5 ? -1 : 1; }
      }
      for (const x of bubbles) {
        if (x.life >= 1) { x.b.visible = false; continue; }
        x.life = Math.min(1, x.life + dt / 2.4);
        x.b.visible = true;
        x.b.position.set(x.side * (0.55 + x.life * 0.2), 3.0 + x.life * 1.1, 0.1);
        x.b.scale.setScalar(Math.sin(Math.min(1, x.life * 1.15) * Math.PI) * 0.9 + 0.05);
      }
    },
  };
  c.rate = 0;
  return o;
}

function hypeKit(c) {
  // antenna headband
  const band = mesh(To(0.54, 0.02, Math.PI, 8, 30), mat(PAL.sage), c.r.head, 0, 0.12, 0.02);
  band.rotation.y = 0;
  const bobs = [-1, 1].map((s) => {
    const a = grp(c.r.head, s * 0.28, 0.55, 0.02);
    mesh(Cy(0.012, 0.012, 0.3, 6), mat(PAL.ink), a, 0, 0.15, 0);
    return { a, ball: mesh(Sp(0.06, 12, 10), mat(PAL.sand, { metalness: 0.3, roughness: 0.4 }), a, 0, 0.32, 0) };
  });
  const meter = grp(c.r.arms[1].hand, 0, -0.08, 0.06);
  const face = grp(meter);
  mesh(Cy(0.2, 0.2, 0.05, 28), mat(PAL.cream), face, 0, 0, 0).rotation.x = Math.PI / 2;
  mesh(To(0.2, 0.025, Math.PI * 2, 8, 32), mat(PAL.sandDark, { metalness: 0.4, roughness: 0.35 }), face);
  mesh(To(0.14, 0.018, Math.PI * 0.35, 6, 12), mat(PAL.heat), face, 0, 0, 0.03).rotation.z = Math.PI * 0.12;
  mesh(To(0.14, 0.018, Math.PI * 0.6, 6, 16), mat(PAL.sage), face, 0, 0, 0.03).rotation.z = Math.PI * 0.4;
  const needle = grp(face, 0, -0.03, 0.04);
  mesh(Bx(0.018, 0.16, 0.01), mat(PAL.ink), needle, 0, 0.08, 0);
  mesh(Sp(0.025, 8, 6), mat(PAL.ink), face, 0, -0.03, 0.04);
  return {
    anim(t, e) {
      const k = clamp01(e), hype = clamp01(e - 1);
      arm(c.r.arms[1], mix(-0.65, -0.3, hype), mix(-0.12, 2.6, hype), mix(-1.3, -0.3, hype));
      arm(c.r.arms[0], mix(0.06, -0.3, hype), mix(0.12, 2.6, hype), mix(-0.2, -0.3, hype));
      meter.rotation.x = -(c.r.arms[1].sh.rotation.x + c.r.arms[1].el.rotation.x) - Math.PI / 2 + 1.4;
      needle.rotation.z = 1.1 - Math.min(2, e) * 1.1 + Math.sin(t * 13) * 0.05 * k;
      bobs.forEach((b, i) => { b.a.rotation.z = Math.sin(t * (2 + 8 * hype) + i * 2) * (0.12 + 0.3 * hype); });
      c.r.head.rotation.y = Math.sin(t * 0.6) * 0.2 * (1 - hype);
    },
  };
}

function cutterKit(c) {
  const sc = grp(c.r.torso, 0, 0.5, 0.58);
  sc.rotation.x = -0.25;
  const blades = [-1, 1].map((s) => {
    const b = grp(sc);
    mesh(Bx(0.06, 0.62, 0.02), M.metal, b, 0, 0.33, 0).geometry.translate(s * 0.015, 0, 0);
    mesh(To(0.075, 0.025, Math.PI * 2, 8, 20), mat(PAL.sage), b, s * 0.06, -0.12, 0);
    return { b, s };
  });
  mesh(Sp(0.03, 10, 8), mat(PAL.sandDark, { metalness: 0.6, roughness: 0.3 }), sc, 0, 0, 0.02);
  return {
    anim(t, e) {
      const k = clamp01(e);
      const open = k > 0.3 ? 0.08 + Math.max(0, Math.sin(t * 7)) * 0.38 : 0.1;
      blades.forEach(({ b, s }) => { b.rotation.z = s * open; });
      arm(c.r.arms[0], -0.62, -0.28, -1.2);
      arm(c.r.arms[1], -0.62, -0.28, -1.2);
      c.r.head.rotation.x = 0.12 * k;
      c.r.torso.rotation.y = Math.sin(t * 0.5) * 0.12 * k;
    },
  };
}

function scribeKit(c) {
  const pad = grp(c.r.torso, -0.14, 0.56, 0.45);
  pad.rotation.set(-0.55, 0.2, 0);
  mesh(Bx(0.3, 0.4, 0.03), mat(PAL.cream), pad);
  mesh(Bx(0.3, 0.08, 0.035), mat(PAL.sandDark), pad, 0, 0.2, 0);
  const lines = [];
  for (let i = 0; i < 5; i++) lines.push(mesh(Bx(0.2, 0.014, 0.005), mat(PAL.sage), pad, -0.02, 0.09 - i * 0.06, 0.02));
  const pencil = grp(c.r.torso, 0.14, 0.62, 0.52);
  pencil.rotation.set(-0.5, 0, -0.5);
  mesh(Cy(0.022, 0.022, 0.32, 6), mat(PAL.sand), pencil);
  mesh(Co(0.022, 0.06, 6), mat(PAL.ink), pencil, 0, -0.19, 0).rotation.x = Math.PI;
  mesh(Cy(0.024, 0.024, 0.04, 6), mat(PAL.heat), pencil, 0, 0.17, 0);
  return {
    anim(t, e) {
      const k = clamp01(e);
      arm(c.r.arms[0], -0.5, -0.3, -1.5);
      const sx = Math.sin(t * 12) * 0.05 * k, sy = Math.cos(t * 6) * 0.03 * k;
      pencil.position.set(0.12 + sx, 0.62 + sy, 0.52);
      arm(c.r.arms[1], -0.55 + sy * 2, -0.22 + sx, -1.45);
      c.r.head.rotation.x = 0.3 * k + 0.05;
      const shown = Math.floor((t * 1.2) % 6);
      lines.forEach((l, i) => { l.visible = k < 0.3 || i < shown; });
    },
  };
}

function announcerKit(c) {
  const beret = grp(c.r.head, 0.08, 0.58, -0.04);
  beret.rotation.z = -0.22;
  mesh(Sp(0.44, 24, 16), mat(PAL.sandDark), beret).scale.set(1.15, 0.32, 1.12);
  mesh(Sp(0.04, 8, 6), mat(PAL.sandDark), beret, 0, 0.14, 0);
  const mega = grp(c.r.torso);
  const horn = grp(mega);
  horn.rotation.x = -Math.PI / 2;
  mesh(Cy(0.08, 0.24, 0.5, 22, 1, true), mat(PAL.sand, { side: THREE.DoubleSide }), horn, 0, 0, 0);
  mesh(To(0.24, 0.025, Math.PI * 2, 8, 30), mat(PAL.sage), horn, 0, -0.25, 0).rotation.x = Math.PI / 2;
  mesh(Cy(0.05, 0.05, 0.16, 10), mat(PAL.ink), mega, 0, -0.12, 0.02);
  const rings = [0, 1, 2].map(() => {
    const m = new THREE.Mesh(To(0.25, 0.015, Math.PI * 2, 6, 30), new THREE.MeshBasicMaterial({ color: PAL.sage, transparent: true }));
    mega.add(m);
    return m;
  });
  return {
    anim(t, e) {
      const k = clamp01(e * 1.2);
      mega.position.set(mix(0.46, 0.05, k), mix(0.2, 1.32, k), mix(0.25, 0.72, k));
      mega.rotation.x = mix(1.2, 0, k);
      arm(c.r.arms[1], mix(0.15, -1.62, k), mix(0.2, -0.38, k), mix(-0.2, -0.95, k));
      arm(c.r.arms[0], mix(0.05, -0.3, k), mix(0.12, 0.5, k), mix(-0.2, -1.9, k));
      rings.forEach((r, i) => {
        const p = (t * 1.3 + i / 3) % 1;
        r.visible = k > 0.7;
        r.position.z = 0.5 + p * 0.8;
        r.scale.setScalar(0.6 + p * 1.6);
        r.material.opacity = 1 - p;
      });
      c.r.head.rotation.y = Math.sin(t * 0.6) * 0.15;
    },
  };
}

function finisherKit(c) {
  const desk = grp(c.g, 0, 0, 0.82);
  mesh(Cy(0.08, 0.1, 1.28, 12), mat(PAL.walnut), desk, 0, 0.64, 0);
  mesh(Cy(0.3, 0.34, 0.05, 20), mat(PAL.walnut), desk, 0, 0.03, 0);
  mesh(Bx(0.82, 0.05, 0.55), mat(PAL.creamDark), desk, 0, 1.3, 0);
  mesh(Bx(0.62, 0.035, 0.42), mat(PAL.ink), desk, 0, 1.345, -0.02);
  const lid = grp(desk, 0, 1.36, 0.19);
  lid.rotation.x = 0.28;
  mesh(Bx(0.62, 0.42, 0.03), mat(PAL.sand), lid, 0, 0.21, 0);
  mesh(Sp(0.05, 12, 8), mat(PAL.cream), lid, 0, 0.21, 0.02).scale.set(1, 1, 0.2);
  const glow = mesh(Bx(0.56, 0.36, 0.005), new THREE.MeshBasicMaterial({ color: PAL.sageLight }), lid, 0, 0.21, -0.018);
  return {
    anim(t, e) {
      const k = clamp01(e);
      for (const a of c.r.arms) arm(a, -0.78, -0.22, -0.85 + Math.sin(t * 14 + a.s * 1.7) * 0.12 * k);
      c.r.head.rotation.x = 0.18 + Math.sin(t * 0.7) * 0.03;
      c.r.head.rotation.y = Math.sin(t * 0.5) * 0.12 * (1 - k);
      glow.material.color.setHex(k > 0.5 ? PAL.sage : PAL.sageLight);
    },
  };
}

/* the crew — each with their own look */
const CREW = {
  manager: () => person({
    skin: 0x7A4A2E, eye: 0x3B2516, hair: { style: 'bun', color: 0x231C18 }, width: 0.95, earrings: PAL.sand, cheeks: 0x8E5238,
    top: { style: 'blazer', color: PAL.sage, accent: PAL.cream }, pants: PAL.olive, shoes: 0x3A2E24,
  }),
  calendar: () => person({
    skin: 0xE8BC98, eye: 0x4F6B7A, hair: { style: 'sidepart', color: 0xBDB6A9 }, beard: true, width: 1.08, browTilt: -0.05,
    glasses: { shape: 'square', color: 0x4A3A2C }, top: { style: 'cardigan', color: PAL.sand, accent: PAL.cream },
    pants: PAL.walnut, shoes: 0x3A2E24,
  }),
  scout: () => person({
    skin: 0xF0C8A8, eye: 0x5F7A4A, hair: { style: 'ponytail', color: 0x8A4B2E }, width: 0.92, cheeks: 0xE7A88A,
    top: { style: 'vest', color: PAL.sageLight, short: true, accent: PAL.sand, accent2: PAL.sandDark }, pants: PAL.walnut, shoes: PAL.sageDark,
  }),
  recorder: () => person({
    skin: 0xC68B5E, eye: 0x3B2516, hair: { style: 'buzz', color: 0x2A2420 }, width: 1.06,
    top: { style: 'hoodie', color: PAL.sageDark }, pants: 0x3A3F33, shoes: PAL.cream,
  }),
  chat: () => person({
    skin: 0xD9A07A, eye: 0x5A3A24, hair: { style: 'curly', color: 0x4A3426, volume: 1.1 },
    top: { style: 'turtle', color: 0xEDE6D6 }, pants: PAL.sage, shoes: PAL.walnut, cheeks: 0xD08A68,
  }),
  detector: () => person({
    skin: 0xF2CDB0, eye: 0x4F6B7A, hair: { style: 'bob', color: 0xD9B676 }, width: 0.94, cheeks: 0xEDA48C,
    top: { style: 'overall', color: PAL.sageLight, short: true, accent: PAL.sandDark }, pants: PAL.sandDark, shoes: PAL.sageDark,
  }),
  cutter: () => person({
    skin: 0xE0B08A, eye: 0x5A3A24, hair: { style: 'quiff', color: 0x7A5A3C }, beard: true, width: 1.1,
    top: { style: 'apron', color: PAL.cream, short: true, accent: PAL.sage, accent2: PAL.sageDark }, pants: PAL.olive, shoes: 0x3A2E24,
  }),
  captioner: () => person({
    skin: 0xEFD0B4, eye: 0x3B2516, hair: { style: 'long', color: 0x1F1A17 }, width: 0.93,
    glasses: { shape: 'round', color: PAL.sand, thin: true, metal: true }, top: { style: 'turtle', color: PAL.sage },
    pants: PAL.sand, shoes: 0x3A2E24, cheeks: 0xE8AE98,
  }),
  titler: () => person({
    skin: 0xF3D2B8, eye: 0x5F7A4A, hair: { style: 'curly', color: 0xB5643A, volume: 0.75 }, freckles: true,
    top: { style: 'scarf', color: PAL.cream, short: true, accent: PAL.sage }, pants: PAL.walnut, shoes: PAL.sandDark,
  }),
  editor: () => person({
    skin: 0x6E4429, eye: 0x3B2516, hair: { style: 'puffs', color: 0x231C18 }, earrings: PAL.sage,
    glasses: { shape: 'square', color: PAL.sageDark }, top: { style: 'denim', color: PAL.sand, accent: PAL.cream },
    pants: PAL.olive, shoes: PAL.cream,
  }),
};
const KITS = {
  manager: directorKit, calendar: archivistKit, scout: lookoutKit, recorder: cameraKit, chat: listenerKit,
  detector: hypeKit, cutter: cutterKit, captioner: scribeKit, titler: announcerKit, editor: finisherKit,
};
function buildCrew(role) {
  const c = CREW[role]();
  c.kit = KITS[role](c);
  return c;
}

/* life: breathing, blinking, sleeping, cheering, being knocked out */
function life(c, t, st, dt) {
  const r = c.r, e = st.energy, hype = clamp01(e - 1);
  r.torso.scale.set(1, 1 + Math.sin(t * 1.8) * 0.012, 1);
  r.root.position.y = hype ? Math.abs(Math.sin(t * 6.5)) * 0.4 * hype : 0;
  // sleeping: head drops and the body slumps
  r.head.rotation.x += st.sleep * 0.45;
  r.torso.rotation.x = st.sleep * 0.14;
  if (st.sleep > 0.05) for (const a of r.arms) arm(a, mix(a.sh.rotation.x, 0.08, st.sleep), mix(a.s * a.sh.rotation.z, 0.08, st.sleep), mix(a.el.rotation.x, -0.25, st.sleep));
  // eyes: blink every few seconds; closed while asleep or knocked out
  c.blinkAt -= dt;
  let open = 1;
  if (c.blinkAt < 0) { open = 0.1; if (c.blinkAt < -0.12) c.blinkAt = 2.5 + Math.random() * 3.5; }
  open = Math.min(open, 1 - 0.94 * Math.max(st.sleep, st.faint));
  for (const eye of r.eyes) eye.scale.y = open;
  r.brows.forEach((b, i) => { b.position.y = 0.22 + hype * 0.05 + (st.status === 'working' ? 0.01 : 0); b.rotation.z = Math.PI / 2 - (i ? 1 : -1) * (0.12 - hype * 0.25); });
  r.smile.visible = hype < 0.3;
  r.open.visible = hype >= 0.3;
  c.g.rotation.z = st.faint * 1.4;
  c.g.position.y = st.faint * 0.32;
}

/* ═══════════════════════════ the grounds ═══════════════════════════ */
// The floor is a slowly drifting ocean: teal-blue noise with soft light streaks and caustics.
// It patches MeshStandardMaterial, so it still takes light and the crew's shadows.
const oceanTime = { value: 0 };
const oceanMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.38, metalness: 0.05 });
oceanMat.onBeforeCompile = (sh) => {
  sh.uniforms.uTime = oceanTime;
  sh.vertexShader = sh.vertexShader
    .replace('#include <common>', '#include <common>\nvarying vec3 vOcean;')
    .replace('#include <begin_vertex>', '#include <begin_vertex>\nvOcean = (modelMatrix * vec4(transformed, 1.0)).xyz;');
  sh.fragmentShader = sh.fragmentShader
    .replace('#include <common>', `#include <common>
      varying vec3 vOcean;
      uniform float uTime;
      float hsh(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
      float vnoise(vec2 p) {
        vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
        return mix(mix(hsh(i), hsh(i + vec2(1, 0)), f.x), mix(hsh(i + vec2(0, 1)), hsh(i + vec2(1, 1)), f.x), f.y);
      }
      float fbm(vec2 p) { float v = 0.0, a = 0.5; for (int i = 0; i < 5; i++) { v += a * vnoise(p); p = p * 2.03 + 17.0; a *= 0.5; } return v; }
      vec3 lin(vec3 c) { return pow(c, vec3(2.2)); }
      vec3 ocean(vec2 p) {
        float t = uTime;
        vec2 q = p * 0.055;
        float n = fbm(q + vec2(t * 0.020, t * 0.012));
        float m = fbm(q * 1.6 + n * 1.4 - vec2(t * 0.016, -t * 0.010));
        vec3 deep = lin(vec3(0.027, 0.341, 0.478));   // #07577A
        vec3 mid  = lin(vec3(0.016, 0.502, 0.639));   // #0480A3
        vec3 teal = lin(vec3(0.071, 0.635, 0.659));   // #12A2A8
        vec3 glint = lin(vec3(0.290, 0.773, 0.871));  // #4AC5DE
        vec3 col = mix(deep, mid, smoothstep(0.30, 0.62, m));
        col = mix(col, teal, smoothstep(0.55, 0.78, m * 0.6 + n * 0.5));
        // long soft streaks, like the light rays in the reference picture
        float s = fbm(vec2(p.x * 0.65, p.y * 0.035 + t * 0.03));
        col = mix(col, glint, smoothstep(0.62, 0.86, s) * 0.28);
        // drifting caustic ripples
        float c = pow(1.0 - abs(sin(m * 14.0 - t * 0.55)), 10.0);
        col += glint * c * 0.10;
        // darker toward the far edge so it sinks into the haze
        col = mix(col, deep * 0.8, smoothstep(14.0, 60.0, length(p)) * 0.6);
        return col;
      }`)
    .replace('#include <color_fragment>', '#include <color_fragment>\nvec3 oceanCol = ocean(vOcean.xz);\ndiffuseColor.rgb = oceanCol;')
    .replace('#include <emissivemap_fragment>', '#include <emissivemap_fragment>\ntotalEmissiveRadiance += oceanCol * 0.35;');
};
const ground = mesh(new THREE.CircleGeometry(90, 96), oceanMat, scene);
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
ground.castShadow = false;

const spotTexture = (() => {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 128;
  const g = cv.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.55, 'rgba(255,255,255,.55)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(cv);
})();

const R = 12.5, N_ST = ORDER.length + 1;
const ringPos = (i, r = R) => {
  const a = -(Math.PI / 6 + (i * (Math.PI * 5 / 3)) / (N_ST - 1));
  return new THREE.Vector3(r * Math.sin(a), 0, r * Math.cos(a));
};
const stations = new Map();
const pickables = [];

function makeLabel(role, title, step) {
  const el = document.createElement('div');
  el.className = 'tag idle';
  el.innerHTML = `<div class="n">${step ? `<i>${step}</i>` : ''}${esc(title)}</div><div class="s">Idle</div><div class="d"></div>`;
  el.addEventListener('click', () => select(role));
  return el;
}

function spot(pos, size = 4.6) {
  const g = grp(scene, pos.x, 0, pos.z);
  const pad = mesh(Cy(1.45, 1.52, 0.06, 48), mat(PAL.cream, { roughness: 0.9 }), g, 0, 0.03, 0);
  pad.receiveShadow = true;
  pad.castShadow = false;
  const spotMat = new THREE.MeshBasicMaterial({ map: spotTexture, color: SPOT.idle[0], transparent: true, depthWrite: false, opacity: SPOT.idle[1] });
  const glow = new THREE.Mesh(new THREE.PlaneGeometry(size, size), spotMat);
  glow.rotation.x = -Math.PI / 2;
  glow.position.y = 0.065;
  glow.renderOrder = 1;
  g.add(glow);
  return { g, spotMat };
}

function addStation(role, i) {
  const pos = ringPos(i);
  const pen = spot(pos);
  const holder = grp(pen.g, 0, 0.06, 0);
  holder.rotation.y = Math.atan2(HOME.pos.x - pos.x, HOME.pos.z - pos.z);
  const crew = buildCrew(role);
  holder.add(crew.g);
  const label = makeLabel(role, TITLES[role], i + 1);
  const tag = new CSS2DObject(label);
  tag.position.set(0, crew.h + 0.75, 0);
  holder.add(tag);
  const st = { role, i, pos, pen, holder, crew, label, status: 'idle', energy: 0.1, target: 0.12, sleep: 0, faint: 0, phase: i * 1.7, base: 1.35 };
  holder.scale.setScalar(st.base);
  pen.g.userData.station = st;
  pickables.push(pen.g);
  stations.set(role, st);
}
ORDER.forEach(addStation);

// the Director's podium in the middle
{
  const pos = new THREE.Vector3();
  const pen = spot(pos, 5.4);
  pen.g.children[0].scale.set(1.25, 1, 1.25);
  mesh(Cy(1.05, 1.18, 0.55, 40), mat(PAL.creamDark, { roughness: 0.8 }), pen.g, 0, 0.3, 0).receiveShadow = true;
  mesh(Cy(1.06, 1.06, 0.06, 40), mat(PAL.sand, { roughness: 0.6 }), pen.g, 0, 0.6, 0).receiveShadow = true;
  const holder = grp(pen.g, 0, 0.63, 0);
  const crew = buildCrew('manager');
  holder.add(crew.g);
  const label = makeLabel('manager', 'Manager', 0);
  const tag = new CSS2DObject(label);
  tag.position.set(0, crew.h + 0.85, 0);
  holder.add(tag);
  const st = { role: 'manager', pos, pen, holder, crew, label, status: 'idle', energy: 0.1, target: 0.12, sleep: 0, faint: 0, phase: 0, base: 1.25 };
  holder.scale.setScalar(st.base);
  pen.g.userData.station = st;
  pickables.push(pen.g);
  stations.set('manager', st);
}

// the clip vault at the end of the line
const vault = (() => {
  const pos = ringPos(N_ST - 1);
  const pen = spot(pos);
  pen.spotMat.color.setHex(PAL.sand);
  pen.spotMat.opacity = 0.55;
  const holder = grp(pen.g, 0, 0.06, 0);
  mesh(Cy(1.0, 1.1, 0.3, 32), mat(PAL.creamDark, { roughness: 0.8 }), holder, 0, 0.15, 0).receiveShadow = true;
  const cubes = new THREE.InstancedMesh(new THREE.BoxGeometry(0.3, 0.3, 0.3), new THREE.MeshStandardMaterial({ roughness: 0.55 }), 30);
  cubes.castShadow = true;
  const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), one = new THREE.Vector3(1, 1, 1), col = new THREE.Color();
  let k = 0, seed = 11;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  for (let layer = 0; layer < 4; layer++) {
    const n = 4 - layer;
    for (let a = 0; a < n; a++) {
      for (let b = 0; b < n; b++) {
        q.setFromEuler(new THREE.Euler(0, rand() * 0.6 - 0.3, 0));
        m4.compose(new THREE.Vector3((a - (n - 1) / 2) * 0.36, 0.46 + layer * 0.31, (b - (n - 1) / 2) * 0.36), q, one);
        cubes.setMatrixAt(k, m4);
        cubes.setColorAt(k, col.setHex([PAL.sage, PAL.sand, PAL.sageLight][k % 3]));
        k++;
      }
    }
  }
  cubes.count = 0;
  holder.add(cubes);
  const label = makeLabel('vault', 'Clip vault', 0);
  label.classList.add('vault');
  label.querySelector('.s').remove();
  const tag = new CSS2DObject(label);
  tag.position.set(0, 2.7, 0);
  holder.add(tag);
  const st = { role: 'vault', pos, pen, holder, label, base: 1 };
  pen.g.userData.station = st;
  pickables.push(pen.g);
  return { cubes, label, st };
})();
const updateVault = () => {
  vault.cubes.count = Math.min(30, S.clips.length);
  vault.label.querySelector('.d').textContent = `${S.clips.length} clip${S.clips.length === 1 ? '' : 's'} ready`;
};

/* the path that links every spot in working order */
const pathMat = new THREE.MeshStandardMaterial({ color: PAL.cream, roughness: 0.6 }); // light so it reads on the water
const pathPoints = Array.from({ length: N_ST }, (_, i) => ringPos(i, R - 2.4).setY(0.04));
const path = new THREE.CatmullRomCurve3(pathPoints);
mesh(new THREE.TubeGeometry(path, 240, 0.06, 8), pathMat, scene).castShadow = false;
const DOTS = 44;
const dots = new THREE.InstancedMesh(Sp(0.075, 8, 6), new THREE.MeshStandardMaterial({ color: PAL.sand, roughness: 0.5 }), DOTS);
scene.add(dots);
const uOf = (role) => (role === 'done' ? 1 : Math.max(0, ORDER.indexOf(role === 'queued' ? 'cutter' : role)) / (N_ST - 1));

/* clips in production: sand orbs that travel the path */
const orbGeo = new THREE.IcosahedronGeometry(0.22, 2);
const orbs = new Map();
function syncOrbs() {
  for (const j of S.jobs.values()) {
    let o = orbs.get(j.id);
    if (!o) {
      if (j.status !== 'active') continue;
      const m = mesh(orbGeo, new THREE.MeshStandardMaterial({ color: PAL.sand, emissive: PAL.sandDark, emissiveIntensity: 0.35, metalness: 0.3, roughness: 0.3, transparent: true }), scene);
      o = { m, u: uOf('detector'), tu: uOf('cutter'), status: 'active', fade: 1 };
      orbs.set(j.id, o);
    }
    o.status = j.status;
    o.tu = j.status === 'done' ? 1 : uOf(j.stage);
  }
  for (const [id, o] of orbs) if (!S.jobs.has(id)) o.status = 'gone';
}

/* ═══════════════════════════ station state from agent data ═══════════════════════════ */
function refreshStation(role) {
  const st = stations.get(role);
  if (!st) return;
  const meta = roleMeta(role), rs = roleState(role);
  st.status = rs.status;
  st.target = ENERGY[rs.status] ?? 0.12;
  const [color, opacity] = SPOT[rs.status] ?? SPOT.idle;
  st.pen.spotMat.color.setHex(color);
  st.spotOpacity = opacity;
  const el = st.label;
  el.className = `tag ${rs.status}`;
  el.querySelector('.n').innerHTML = `${meta.step && role !== 'manager' ? `<i>${meta.step}</i>` : ''}${esc(meta.title)}${rs.count > 1 ? ` <i>×${rs.count}</i>` : ''}`;
  el.querySelector('.s').innerHTML = rs.status === 'standby' ? `Asleep <span class="zz">z z</span>` : esc(STATUS[rs.status] || rs.status);
  el.querySelector('.d').textContent = rs.task || '';
}
const refreshAll = () => { ['manager', ...ORDER].forEach(refreshStation); updateVault(); };

/* ═══════════════════════════ camera focus ═══════════════════════════ */
let tween = null;
const ease = (x) => 1 - Math.pow(1 - x, 3);
function flyTo(target, pos) {
  tween = { t: 0, fromT: controls.target.clone(), fromP: camera.position.clone(), toT: target.clone(), toP: pos.clone() };
}
function focusStation(st) {
  // On narrow screens the panel is a bottom sheet, so stand further back and aim below the character.
  const narrow = innerWidth < 760;
  if (st.role === 'manager') {
    return flyTo(new THREE.Vector3(0, narrow ? 0.5 : 2.8, 0), new THREE.Vector3(0, narrow ? 9 : 5.5, narrow ? 15 : 9.5));
  }
  const out = new THREE.Vector3(HOME.pos.x - st.pos.x, 0, HOME.pos.z - st.pos.z).normalize();
  flyTo(st.pos.clone().setY(narrow ? 0.9 : 2.0), st.pos.clone().addScaledVector(out, narrow ? 13 : 8).setY(narrow ? 6.5 : 4.2));
}
const goHome = () => flyTo(HOME.target, HOME.pos);

/* ═══════════════════════════ picking ═══════════════════════════ */
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
let hovered = null, downAt = null, pointerDown = false;
function pick(e) {
  ptr.set((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  for (const h of ray.intersectObjects(pickables, true)) {
    let o = h.object;
    while (o && !o.userData.station) o = o.parent;
    if (o) return o.userData.station;
  }
  return null;
}
renderer.domElement.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; hideHint(); });
renderer.domElement.addEventListener('pointerup', (e) => {
  if (!downAt) return;
  const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]);
  downAt = null;
  if (moved > 6) return;
  const st = pick(e);
  if (st) select(st.role);
});
renderer.domElement.addEventListener('pointermove', (e) => {
  if (e.buttons) return;
  hovered = pick(e);
  renderer.domElement.style.cursor = hovered ? 'pointer' : '';
});
renderer.domElement.addEventListener('wheel', hideHint, { passive: true });
addEventListener('pointerdown', () => { pointerDown = true; }, true);
addEventListener('pointerup', () => { pointerDown = false; }, true);
function hideHint() { $('#hint').classList.add('gone'); }

function select(role) {
  hideHint();
  if (role === 'vault') { openPanel('clips'); focusStation(vault.st); return; }
  if (role === 'manager') { openChat(); focusStation(stations.get(role)); return; }
  openPanel('agent', role);
  focusStation(stations.get(role));
}

/* ═══════════════════════════ animation loop ═══════════════════════════ */
const clock = new THREE.Clock();
const dummy = new THREE.Object3D();
let lastPanel = 0;
function frame() {
  const dt = Math.min(clock.getDelta(), 0.1), now = clock.elapsedTime;
  oceanTime.value = now;

  // shrink the header as soon as the view is scrolled away from its starting distance, so it stays clear of name tags
  const compact = Math.abs(camera.position.distanceTo(controls.target) - homeDist) > 1.5;
  if (compact !== headerCompact) { headerCompact = compact; $('.bar').classList.toggle('compact', compact); }

  let activity = 0;
  for (const st of stations.values()) {
    st.energy += (st.target - st.energy) * Math.min(1, dt * 2.5);
    st.sleep += ((st.status === 'standby' ? 1 : 0) - st.sleep) * Math.min(1, dt * 2);
    st.faint += ((st.status === 'error' ? 1 : 0) - st.faint) * Math.min(1, dt * 2.5);
    activity = Math.max(activity, Math.min(st.energy, 1));
    const t = now + st.phase;
    // reset the head tilt every frame: life() adds the sleepy droop on top, and not every kit sets it
    st.crew.r.head.rotation.x = 0;
    st.crew.kit.anim(t, st.energy, dt);
    life(st.crew, t, st, dt);
    const hs = st.base * (hovered === st ? 1.05 : 1);
    st.holder.scale.setScalar(st.holder.scale.x + (hs - st.holder.scale.x) * Math.min(1, dt * 10));
    const base = st.spotOpacity ?? 0.35;
    st.pen.spotMat.opacity = st.status === 'alert' ? base * (0.7 + 0.3 * Math.sin(now * 8))
      : st.status === 'working' ? base * (0.85 + 0.15 * Math.sin(now * 3))
        : st.status === 'error' ? base * (0.6 + 0.4 * Math.sin(now * 5)) : base;
  }
  const vh = vault.st.holder;
  vh.scale.setScalar(vh.scale.x + ((hovered === vault.st ? 1.05 : 1) - vh.scale.x) * Math.min(1, dt * 10));

  // the Director turns to watch whoever is busiest
  const mgr = stations.get('manager');
  let busiest = null;
  for (const st of stations.values()) if (st.role !== 'manager' && st.energy > 0.6 && (!busiest || st.energy > busiest.energy)) busiest = st;
  const look = busiest ? Math.atan2(busiest.pos.x, busiest.pos.z) : Math.atan2(camera.position.x, camera.position.z);
  let diff = look - mgr.holder.rotation.y;
  diff = Math.atan2(Math.sin(diff), Math.cos(diff));
  mgr.holder.rotation.y += diff * Math.min(1, dt * 1.5);

  // flowing dots along the path: faster when the crew is busy
  const speed = 0.006 + 0.03 * activity;
  for (let k = 0; k < DOTS; k++) {
    const u = (k / DOTS + now * speed) % 1;
    dummy.position.copy(path.getPointAt(u)).setY(0.1);
    dummy.updateMatrix();
    dots.setMatrixAt(k, dummy.matrix);
  }
  dots.instanceMatrix.needsUpdate = true;

  // clip orbs
  for (const [id, o] of orbs) {
    o.u += (o.tu - o.u) * Math.min(1, dt * 1.2);
    const p = path.getPointAt(Math.min(1, Math.max(0, o.u)));
    o.m.position.set(p.x, 0.75 + Math.sin(now * 3 + o.u * 9) * 0.12, p.z);
    o.m.rotation.y += dt * 2;
    o.m.rotation.x += dt * 1.3;
    const leaving = o.status === 'failed' || o.status === 'gone' || (o.status === 'done' && o.u > 0.985);
    if (leaving) {
      o.fade -= dt * 0.8;
      o.m.scale.setScalar(Math.max(0.01, o.fade));
      o.m.material.opacity = Math.max(0, o.fade);
      if (o.fade <= 0) { scene.remove(o.m); o.m.material.dispose(); orbs.delete(id); }
    }
  }

  if (tween) {
    tween.t = Math.min(1, tween.t + dt / 1.1);
    const k = ease(tween.t);
    controls.target.lerpVectors(tween.fromT, tween.toT, k);
    camera.position.lerpVectors(tween.fromP, tween.toP, k);
    if (tween.t >= 1) tween = null;
  }
  controls.update();

  if (panelDirty && !pointerDown && performance.now() - lastPanel > 300) {
    panelDirty = false;
    lastPanel = performance.now();
    renderPanel(false);
  }

  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
  requestAnimationFrame(frame);
}

// Frame the scene as if the canvas started below the solid header, so the crew is centred in the visible area.
function fitCamera() {
  const head = innerWidth < 760 ? 76 : 96;
  camera.aspect = innerWidth / innerHeight;
  // same zoom as before, just slid down by half the header height
  camera.setViewOffset(innerWidth, innerHeight, 0, -head / 2, innerWidth, innerHeight);
  camera.updateProjectionMatrix();
}
fitCamera();
addEventListener('resize', () => {
  fitCamera();
  renderer.setSize(innerWidth, innerHeight);
  labelRenderer.setSize(innerWidth, innerHeight);
});

/* ═══════════════════════════ overlay UI ═══════════════════════════ */
const clockStr = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const dayTime = (t) => new Date(t * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
function span(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  return `${Math.floor(sec / 86400)}d`;
}
const ago = (t) => `${span(Date.now() / 1000 - t)} ago`;

function toast(text, bad = false) {
  const el = $('#toast');
  el.textContent = text;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = 'toast'), 3000);
}

async function api(method, url, body) {
  const res = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep status text */ }
    throw new Error(detail);
  }
  return res.json();
}

function renderStats() {
  const live = [...S.streamers.values()].filter((s) => s.live).length;
  const inProd = [...S.jobs.values()].filter((j) => j.status === 'active').length;
  const busy = [...S.agents.values()].filter((a) => a.status === 'working' || a.status === 'alert').length;
  $('#stats').innerHTML = S.connected
    ? `<span class="live"><b>${live}</b> live</span><span><b>${busy}</b> of <b>${S.agents.size}</b> crew busy</span><span><b>${inProd}</b> in production</span>`
    : '<span class="off">Reconnecting…</span>';
  $('#clipCount').textContent = S.clips.length;
}

/* panels */
let panelDirty = false;
function openPanel(kind, arg) {
  S.panel = { kind, arg };
  renderPanel(true);
  $('#panel').classList.add('show');
  $('#panel').setAttribute('aria-hidden', 'false');
}
function closePanel() {
  if (!S.panel) return;
  S.panel = null;
  $('#panel').classList.remove('show');
  $('#panel').setAttribute('aria-hidden', 'true');
  goHome();
}
function markPanel(kind, role) {
  const p = S.panel;
  if (p && p.kind === kind && (role === undefined || p.arg === role)) panelDirty = true;
}
function renderPanel(full) {
  const p = S.panel;
  if (!p) return;
  const el = $('#panel');
  el.classList.toggle('wide', p.kind === 'chat');
  if (p.kind === 'agent') el.innerHTML = agentPanel(p.arg);
  else if (p.kind === 'chat') {
    if (full || !$('#chatLog')) el.innerHTML = chatShell();
    renderChat();
  } else if (p.kind === 'clips') el.innerHTML = clipsPanel();
  else if (p.kind === 'streamers') {
    if (full || !$('#slist')) el.innerHTML = streamersShell();
    $('#slist').innerHTML = streamerRows();
  } else if (p.kind === 'settings' && full) el.innerHTML = settingsPanel();
}
const head = (kicker, title) => `<div class="p-head"><div><div class="kicker">${kicker}</div><h2>${title}</h2></div><button class="x" data-act="close" aria-label="Close">✕</button></div>`;

function agentPanel(role) {
  const meta = roleMeta(role), list = agentsOf(role);
  const [persona, line] = PERSONA[role];
  const now = Date.now() / 1000;
  const logs = S.logs.filter((l) => l.agent.split(':')[0] === role).slice(-8).reverse();
  const inst = list.length
    ? list.map((a) => `<div class="inst">
        <div class="row"><span class="st st-${a.status}">${STATUS[a.status] || a.status}</span>
          ${a.streamer ? `<b>${esc(streamerName(a.streamer))}</b>` : ''}
          ${role !== 'manager' ? `<button class="link" data-act="restart" data-name="${esc(a.name)}">Restart</button>` : ''}</div>
        <div class="task">${esc(a.task || '—')}</div>
        <div class="kv"><span>up <b>${span(now - a.started)}</b></span>${a.restarts ? `<span>restarts <b>${a.restarts}</b></span>` : ''}
          ${Object.entries(a.stats || {}).map(([k, v]) => `<span>${esc(k.replace(/_/g, ' '))} <b>${esc(v)}</b></span>`).join('')}</div>
        ${a.error ? `<div class="err">${esc(a.error)}</div>` : ''}</div>`).join('')
    : `<div class="inst muted">Asleep. The Manager wakes one up for every stream that goes live.</div>`;
  return `${head(`${role === 'manager' ? 'Runs the crew' : `Step ${meta.step} of 9`} · ${persona}`, esc(meta.title))}
    <p class="lead">${esc(meta.desc)}</p><p class="why">${esc(line)}</p>
    ${inst}
    <div class="sub">Recent</div>
    <div class="logs">${logs.length ? logs.map((l) => `<div class="${l.level}"><time>${clockStr(l.t)}</time><span>${esc(l.msg)}</span></div>`).join('') : '<div class="muted">Nothing yet</div>'}</div>`;
}

function streamersShell() {
  return `${head('Watch list', 'Streamers')}
    <form id="addForm" class="add" autocomplete="off">
      <input id="addInput" placeholder="Channel link or name" aria-label="Channel link or name">
      <select id="addPlatform" aria-label="Platform"><option value="twitch">Twitch</option><option value="youtube">YouTube</option></select>
      <button class="gold" type="submit">Add</button>
    </form><div id="slist"></div>`;
}
function streamerRows() {
  const now = Date.now() / 1000;
  const list = [...S.streamers.values()].sort((a, b) => (b.live ? 1 : 0) - (a.live ? 1 : 0) || a.name.localeCompare(b.name));
  if (!list.length) return '<div class="empty">No streamers yet. Add a channel and the crew gets to work.</div>';
  return list.map((s) => {
    const next = s.calendar?.upcoming?.find((u) => u.start > now);
    let sub;
    if (!s.enabled) sub = 'Paused';
    else if (s.live) sub = esc(s.title || 'Live');
    else if (s.note) sub = esc(s.note);
    else if (next) sub = `Next expected ${dayTime(next.start)}`;
    else if (s.last_check) sub = `Offline · checked ${ago(s.last_check)}`;
    else sub = 'Checking…';
    return `<div class="srow"><span class="dot ${s.live ? 'live' : ''}"></span>
      <div class="sinfo"><b>${esc(s.name)}</b><small>${s.platform}</small><div class="muted" title="${esc(s.calendar?.summary || '')}">${sub}</div></div>
      <div class="sact">
        ${s.live ? `<button class="link" data-act="clip" data-id="${s.id}">Clip now</button>` : ''}
        <button class="link" data-act="toggle" data-id="${s.id}">${s.enabled ? 'Pause' : 'Resume'}</button>
        <button class="link danger" data-act="remove" data-id="${s.id}">Remove</button>
      </div></div>`;
  }).join('');
}

function clipsPanel() {
  const grid = S.clips.length
    ? `<div class="grid">${S.clips.map((c) => {
      const ed = S.edits[c.id];
      const busy = ed && ['preparing', 'transcribing', 'rendering'].includes(ed.status)
        ? `<em class="busy">${ed.status === 'rendering' ? `Rendering ${Math.round((ed.progress || 0) * 100)}%` : 'Preparing…'}</em>` : '';
      return `<div class="clip">
        <button class="th" data-act="play" data-id="${esc(c.id)}" aria-label="Play ${esc(c.title)}" style="background-image:url('/media/${encodeURIComponent(c.thumb)}')"><span>${Math.round(c.duration)}s</span>${busy}</button>
        <div class="clip-row"><b>${esc(c.title)}</b><button class="edit-btn" data-act="edit" data-id="${esc(c.id)}">Edit</button></div>
        <small>${esc(c.streamer_name)} · ${ago(c.created)}${c.edited ? ' · edited' : ''}</small></div>`;
    }).join('')}</div>`
    : '<div class="empty">No clips yet. Finished Shorts and Reels collect here.</div>';
  return `${head('The vault', 'Clips')}${grid}`;
}

function openClip(id) {
  const c = S.clips.find((x) => x.id === id);
  if (!c) return;
  const tags = (c.hashtags || []).join(' ');
  const sig = c.signal || {};
  $('#modal').innerHTML = `<div class="sheet">
    <video src="/media/${encodeURIComponent(c.file)}" controls autoplay playsinline></video>
    <div class="side">
      <div class="kicker">${esc(c.streamer_name)} · ${Math.round(c.duration)}s</div>
      <h3>${esc(c.title)}</h3>
      <div class="copy"><div class="row"><span class="kicker">Title</span><button class="link" data-act="copy" data-text="${esc(c.title)}">Copy</button></div>${esc(c.title)}</div>
      <div class="copy"><div class="row"><span class="kicker">Caption</span><button class="link" data-act="copy" data-text="${esc(c.description + '\n\n' + tags)}">Copy</button></div>${esc(c.description)} ${esc(tags)}</div>
      <div class="copy muted">${sig.reason === 'manual' ? 'Clipped by hand.' : `Chat hit ${sig.peak_rate} msg/s against a normal ${sig.base}.`}${c.virality != null ? ` Virality ${c.virality}/100.` : ''}</div>
      <div class="actions">
        <a class="gold" href="/media/${encodeURIComponent(c.file)}" download="${esc(c.file)}">Download</a>
        <button class="link" data-act="edit" data-id="${esc(c.id)}">Edit</button>
        <button class="link danger" data-act="delclip" data-id="${esc(c.id)}">Delete</button>
        <button class="link" data-act="closemodal" style="margin-left:auto">Close</button>
      </div>
    </div></div>`;
  $('#modal').classList.add('show');
}
// Windows keeps a file locked while a <video> still has it open, so stop and unload any player first.
const releaseVideos = () => document.querySelectorAll('#modal video').forEach((v) => { v.pause(); v.removeAttribute('src'); v.load(); });
const closeModal = () => { releaseVideos(); ED = null; $('#modal').classList.remove('show', 'wide'); $('#modal').innerHTML = ''; };

/* ═══════════════════════════ Manager chat ═══════════════════════════ */
S.mchat = { session: null, requirements: [], mode: 'basic', note: '', sending: false, showReqs: false };
S.edits = {};

async function openChat() {
  openPanel('chat');
  try {
    let v = await api('GET', '/api/manager/chat');
    if (!v.session || v.session.status !== 'open') v = await api('POST', '/api/manager/chat/start');
    setChat(v);
    $('#chatText')?.focus();
  } catch (err) { toast(err.message, true); }
}
function setChat(v) {
  Object.assign(S.mchat, { session: v.session, requirements: v.requirements, mode: v.mode, note: v.note });
  if (S.panel?.kind === 'chat') renderChat();
}
function chatShell() {
  return `${head('Requirements session · The Director', 'Manager')}
    <div class="chat-meta"><span id="chatMode" class="badge"></span>
      <button class="link" data-act="chat-reqs">Saved requirements (<b id="reqCount">0</b>)</button>
      <button class="link" data-act="chat-status">Crew status</button></div>
    <div id="reqList" class="reqs" hidden></div>
    <div id="chatLog" class="chat-log" aria-live="polite"></div>
    <form id="chatForm" class="chat-input" autocomplete="off">
      <textarea id="chatText" rows="2" placeholder="Tell the Manager what you want…" aria-label="Message to the Manager"></textarea>
      <button class="send" type="submit" aria-label="Send">↑</button>
    </form>
    <div class="chat-foot"><button class="link" data-act="chat-new">New session</button>
      <button class="link danger" data-act="chat-end">End session</button></div>`;
}
const chatText = (t) => esc(t).replace(/\n/g, '<br>');
function renderChat() {
  const log = $('#chatLog');
  if (!log) return;
  const m = S.mchat, s = m.session, open = s?.status === 'open';
  $('#chatMode').textContent = m.mode === 'claude' ? 'Powered by Claude' : 'Built-in interviewer';
  $('#chatMode').title = m.mode === 'claude' ? '' : 'Add ANTHROPIC_API_KEY to .env for smarter, AI-driven questions.';
  $('#reqCount').textContent = m.requirements.length;
  const rl = $('#reqList');
  rl.hidden = !m.showReqs;
  rl.innerHTML = m.requirements.length
    ? m.requirements.map((r) => `<div class="req"><i>${esc(r.category.replace('_', ' '))}</i><span>${esc(r.text)}</span>
        <button class="x sm" data-act="req-del" data-id="${esc(r.id)}" aria-label="Remove requirement">✕</button></div>`).join('')
    : '<div class="muted">Nothing saved yet.</div>';
  const msgs = (s?.messages || []).map((x) => `<div class="msg ${x.role}">
      <div class="bubble">${chatText(x.text)}</div>
      ${x.actions?.length ? `<div class="acts">${x.actions.map((a) => `<span>✓ ${esc(a)}</span>`).join('')}</div>` : ''}</div>`).join('');
  log.innerHTML = msgs
    + (m.sending ? '<div class="msg manager"><div class="bubble typing"><i></i><i></i><i></i></div></div>' : '')
    + (m.note ? `<div class="chat-note">${esc(m.note)}</div>` : '')
    + (s && !open ? '<div class="chat-note">This session is over. Start a new one any time.</div>' : '');
  log.scrollTop = log.scrollHeight;
  $('#chatText').disabled = !open || m.sending;
  $('#chatForm .send').disabled = !open || m.sending;
  $('#chatText').placeholder = open ? 'Tell the Manager what you want…' : 'Session ended';
}
async function sendChat() {
  const box = $('#chatText'), text = box.value.trim();
  if (!text || S.mchat.sending) return;
  box.value = '';
  S.mchat.session?.messages.push({ role: 'owner', text, actions: [] });
  S.mchat.sending = true;
  renderChat();
  try { setChat(await api('POST', '/api/manager/chat/message', { text })); }
  catch (err) { toast(err.message, true); }
  finally { S.mchat.sending = false; renderChat(); $('#chatText')?.focus(); }
}

/* ═══════════════════════════ clip editor ═══════════════════════════ */
let ED = null;
const SCHEMES = [
  { name: 'Classic', title_color: '#141414', title_box: true, title_box_color: '#FFFFFF', cap_color: '#FFFFFF', cap_highlight: '#FFE500', cap_outline: '#000000' },
  { name: 'Masuga', title_color: '#CACE00', title_box: true, title_box_color: '#1F3635', cap_color: '#FCFBF4', cap_highlight: '#CACE00', cap_outline: '#1F3635' },
  { name: 'Ocean', title_color: '#FCFBF4', title_box: true, title_box_color: '#025A5C', cap_color: '#FFFFFF', cap_highlight: '#7BA3CE', cap_outline: '#07577A' },
  { name: 'Heat', title_color: '#FFFFFF', title_box: true, title_box_color: '#E73F3C', cap_color: '#FFFFFF', cap_highlight: '#E73F3C', cap_outline: '#000000' },
  { name: 'Blush', title_color: '#1F3635', title_box: true, title_box_color: '#E9BAC3', cap_color: '#FFFFFF', cap_highlight: '#E9BAC3', cap_outline: '#1F3635' },
  { name: 'Outline', title_color: '#FFFFFF', title_box: false, title_box_color: '#000000', cap_color: '#FFFFFF', cap_highlight: '#FFE500', cap_outline: '#000000' },
];
const cssFont = (f) => (f === 'Gondens DEMO' ? "'Gondens', sans-serif" : `'${f}', sans-serif`);
const fmtT = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;

function chunkWords(words) {   // same grouping as the renderer: up to 3 words / 16 chars, break on pauses and sentence ends
  const out = []; let cur = [];
  for (const w of words) {
    if (cur.length && (cur.length >= 3 || cur.reduce((n, x) => n + x.w.length, 0) + w.w.length > 16
      || w.s - cur[cur.length - 1].e > 0.55 || '.?!'.includes(cur[cur.length - 1].w.slice(-1)))) { out.push(cur); cur = []; }
    cur.push(w);
  }
  if (cur.length) out.push(cur);
  return out.map((c, i) => ({ words: c, s: c[0].s, e: Math.min(c[c.length - 1].e + 0.3, out[i + 1]?.[0].s ?? 1e9) }));
}

async function openEditor(id) {
  closeModal();
  $('#modal').innerHTML = '<div class="editor loading"><div class="ed-wait" id="edWait">Getting the clip ready…</div></div>';
  $('#modal').classList.add('show', 'wide');
  try {
    const info = await api('GET', `/api/clips/${encodeURIComponent(id)}/editor`);
    if (!$('#edWait')) return;   // closed while loading
    ED = { ...info, style: { ...info.style }, chunks: chunkWords(info.meta.words || []) };
    renderEditor();
  } catch (err) { closeModal(); toast(err.message, true); }
}

const field = (label, inner) => `<label class="ef"><span>${label}</span>${inner}</label>`;
const fontSel = (k) => `<select data-k="${k}">${ED.fonts.map((f) => `<option value="${esc(f)}" ${ED.style[k] === f ? 'selected' : ''} style="font-family:${cssFont(f)}">${esc(f === 'Gondens DEMO' ? 'Gondens' : f)}</option>`).join('')}</select>`;
const rng = (k, min, max, step, unit = '') => `<div class="rng"><input type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${ED.style[k]}"><output data-o="${k}">${ED.style[k]}${unit}</output></div>`;
const col = (k) => `<input type="color" data-k="${k}" value="${ED.style[k]}">`;
const chk = (k, label) => `<label class="ck"><input type="checkbox" data-k="${k}" ${ED.style[k] ? 'checked' : ''}> ${label}</label>`;

function renderEditor() {
  const st = ED.style, m = ED.meta, c = S.clips.find((x) => x.id === ED.id) || {};
  $('#modal').innerHTML = `<div class="editor">
    <div class="ed-stage">
      <div class="pv" id="pv">
        <video class="pv-bg" muted playsinline preload="auto" src="${ED.source}"></video>
        <div class="pv-fgwrap"><video class="pv-fg" playsinline preload="auto" src="${ED.source}"></video></div>
        <div class="pv-title"><span></span></div>
        <div class="pv-cap"></div>
        <div class="pv-credit">${esc(m.platform === 'twitch' ? 'twitch.tv/' + m.handle : 'youtube.com/' + m.handle)}</div>
      </div>
      <div class="ed-transport"><button type="button" class="ed-play" data-act="ed-play" aria-label="Play">▶</button>
        <input type="range" id="edSeek" min="0" max="${m.duration}" step="0.05" value="${st.trim_start}" aria-label="Seek"><span id="edTime">0:00</span></div>
    </div>
    <div class="ed-side">
      ${head('Edit clip', esc(c.title || st.title))}
      ${ED.legacy ? '<p class="ed-note">This clip was made before editing existed, so its footage was cut back out of the finished video and the captions were transcribed again.</p>' : ''}
      <div class="sub">Colour scheme</div>
      <div class="schemes">${SCHEMES.map((s, i) => `<button type="button" class="scheme" data-act="ed-scheme" data-id="${i}" title="${s.name}">
        <i style="background:${s.title_box ? s.title_box_color : '#444'};color:${s.title_color}">Aa</i><b style="background:${s.cap_highlight}"></b><span>${s.name}</span></button>`).join('')}</div>
      <div class="sub">Title</div>
      ${field('Text', `<input type="text" data-k="title" maxlength="90" value="${esc(st.title)}">`)}
      <div class="row2">${field('Font', fontSel('title_font'))}${field('Position', `<select data-k="title_pos">
        <option value="above" ${st.title_pos === 'above' ? 'selected' : ''}>Above the stream</option>
        <option value="top" ${st.title_pos === 'top' ? 'selected' : ''}>Top of frame</option>
        <option value="center" ${st.title_pos === 'center' ? 'selected' : ''}>Centre</option></select>`)}</div>
      ${field('Size', rng('title_size', 28, 120, 1, 'px'))}
      <div class="row3">${field('Text', col('title_color'))}${field(st.title_box ? 'Box' : 'Outline', col('title_box_color'))}${chk('title_box', 'Box behind')}</div>
      <div class="sub">Captions ${m.words?.length ? '' : '<em class="muted">(no speech found)</em>'}</div>
      <div class="row2">${chk('captions', 'Show captions')}${chk('cap_upper', 'ALL CAPS')}</div>
      ${field('Font', fontSel('cap_font'))}
      ${field('Size', rng('cap_size', 30, 110, 1, 'px'))}
      <div class="row3">${field('Text', col('cap_color'))}${field('Highlight', col('cap_highlight'))}${field('Outline', col('cap_outline'))}</div>
      <div class="sub">Video</div>
      ${field('Background', `<div class="seg">${[['blur', 'Blur'], ['blur-dark', 'Dark blur'], ['solid', 'Solid']].map(([v, l]) =>
        `<button type="button" data-act="ed-bg" data-id="${v}" class="${st.background === v ? 'on' : ''}">${l}</button>`).join('')}</div>`)}
      ${st.background === 'solid' ? field('Background colour', col('bg_color')) : ''}
      ${field('Stream size', rng('zoom', 0.7, 1.6, 0.05, '×'))}
      <div class="row2">${field('Start at', rng('trim_start', 0, Math.max(0, m.duration - 5), 0.1, 's'))}${field('End at', rng('trim_end', 5, m.duration, 0.1, 's'))}</div>
      <div class="ed-len" id="edLen"></div>
      <div class="ed-progress" id="edProg" hidden><div><i></i></div><span></span></div>
      <div class="ed-actions"><button type="button" class="link" data-act="closemodal">Cancel</button>
        <button type="button" class="gold" data-act="ed-render">Save &amp; render</button></div>
    </div></div>`;
  const fg = $('#pv .pv-fg'), bg = $('#pv .pv-bg');
  fg.addEventListener('loadedmetadata', () => { fg.currentTime = st.trim_start; bg.currentTime = st.trim_start; });
  fg.addEventListener('play', () => { bg.play().catch(() => {}); $('.ed-play').textContent = '❚❚'; });
  fg.addEventListener('pause', () => { bg.pause(); $('.ed-play').textContent = '▶'; });
  updatePreview();
  requestAnimationFrame(editorLoop);
}

function layoutOf() {
  const m = ED.meta, st = ED.style;
  const fgW = Math.floor(1080 * (m.base_zoom || 1) * st.zoom / 2) * 2;
  const fgH = Math.min(Math.floor(m.src_h * fgW / m.src_w / 2) * 2, 1300);
  return { fgW, fgH, fgTop: (1920 - fgH) / 2, vidH: m.src_h * fgW / m.src_w };
}

function updatePreview() {
  const pv = $('#pv');
  if (!pv || !ED) return;
  const st = ED.style, k = pv.clientWidth / 1080, L = layoutOf();
  const wrapW = Math.min(L.fgW, 1080);
  Object.assign(pv.querySelector('.pv-fgwrap').style, { left: `${(1080 - wrapW) / 2 * k}px`, top: `${L.fgTop * k}px`, width: `${wrapW * k}px`, height: `${L.fgH * k}px` });
  Object.assign(pv.querySelector('.pv-fg').style, { width: `${L.fgW * k}px`, height: `${L.vidH * k}px`, left: `${(wrapW - L.fgW) / 2 * k}px`, top: `${(L.fgH - L.vidH) / 2 * k}px` });
  const bg = pv.querySelector('.pv-bg');
  bg.style.display = st.background === 'solid' ? 'none' : '';
  bg.style.filter = `blur(${10 * k * 4}px) brightness(${st.background === 'blur-dark' ? 0.6 : 0.78}) saturate(1.15)`;
  pv.style.background = st.background === 'solid' ? st.bg_color : '#000';

  const t = pv.querySelector('.pv-title'), span = t.firstElementChild, size = st.title_size;
  span.textContent = st.title;
  // match the renderer: same outline/box formula, and libass line height = em / font scale
  const out = st.title_box ? Math.max(8, Math.round(size * 0.28)) : Math.max(3, Math.round(size * 0.1));
  const lh = 1 / (ED.font_scale?.[st.title_font] || 0.75);
  Object.assign(span.style, {
    fontFamily: cssFont(st.title_font), fontSize: `${size * k}px`, color: st.title_color, lineHeight: lh,
    background: st.title_box ? st.title_box_color : 'none', padding: st.title_box ? `${out * k * 0.35}px ${out * k}px` : '0',
    WebkitTextStroke: st.title_box ? '0' : `${out * 2 * k}px ${st.title_box_color}`,
  });
  Object.assign(t.style, { maxWidth: `${960 * k}px`, lineHeight: lh, top: '', bottom: '', transform: '' });
  if (st.title_pos === 'top') t.style.top = `${150 * k}px`;
  else if (st.title_pos === 'center') { t.style.top = '50%'; t.style.transform = 'translateY(-50%)'; }
  else t.style.bottom = `${(1920 - L.fgTop + 46) * k}px`;

  const cap = pv.querySelector('.pv-cap');
  Object.assign(cap.style, {
    top: `${(L.fgTop + L.fgH + 70) * k}px`, fontFamily: cssFont(st.cap_font), fontSize: `${st.cap_size * k}px`, color: st.cap_color,
    lineHeight: 1 / (ED.font_scale?.[st.cap_font] || 0.75),
    WebkitTextStroke: `${Math.max(3, Math.round(st.cap_size * 0.12)) * 2 * k}px ${st.cap_outline}`, display: st.captions ? '' : 'none',
  });
  pv.querySelector('.pv-credit').style.fontSize = `${34 * k}px`;
  ED.lastChunk = null;
  const len = st.trim_end - st.trim_start;
  $('#edLen').textContent = `Final length ${Math.round(len)}s${len < 45 || len > 90 ? ' · Shorts and Reels work best at 45–90s' : ''}`;
}

function editorLoop() {
  if (!ED || !$('#pv')) return;
  const fg = $('#pv .pv-fg'), bg = $('#pv .pv-bg'), st = ED.style;
  const t = fg.currentTime || 0;
  if (!fg.paused && (t >= st.trim_end || t < st.trim_start - 0.5)) fg.currentTime = st.trim_start;
  if (Math.abs((bg.currentTime || 0) - t) > 0.3) bg.currentTime = t;
  const seek = $('#edSeek');
  if (document.activeElement !== seek) seek.value = t;
  $('#edTime').textContent = `${fmtT(t)} / ${fmtT(ED.meta.duration)}`;
  const ch = ED.chunks.find((c) => t >= c.s && t < c.e);
  const wi = ch ? ch.words.findIndex((w, i) => t >= w.s && (i === ch.words.length - 1 || t < ch.words[i + 1].s)) : -1;
  const key = ch ? `${ch.s}:${wi}:${st.cap_highlight}:${st.cap_upper}` : '';
  if (key !== ED.lastChunk) {
    ED.lastChunk = key;
    $('#pv .pv-cap').innerHTML = ch ? ch.words.map((w, i) => {
      const txt = esc(st.cap_upper ? w.w.toUpperCase() : w.w);
      return i === wi ? `<b style="color:${st.cap_highlight}">${txt}</b>` : txt;
    }).join(' ') : '';
  }
  requestAnimationFrame(editorLoop);
}

function editorInput(el) {
  const k = el.dataset.k;
  let v = el.type === 'checkbox' ? el.checked : el.value;
  if (el.type === 'range') v = Number(v);
  ED.style[k] = v;
  const o = document.querySelector(`[data-o="${k}"]`);
  if (o) o.textContent = `${v}${k.endsWith('size') ? 'px' : k === 'zoom' ? '×' : k.startsWith('trim') ? 's' : ''}`;
  if (k === 'trim_start' && ED.style.trim_end - v < 5) ED.style.trim_end = Math.min(ED.meta.duration, v + 5);
  if (k === 'trim_end' && v - ED.style.trim_start < 5) ED.style.trim_start = Math.max(0, v - 5);
  if (k.startsWith('trim')) { const fg = $('#pv .pv-fg'); fg.currentTime = k === 'trim_start' ? ED.style.trim_start : Math.max(ED.style.trim_start, ED.style.trim_end - 2); }
  if (k === 'title_box') { renderEditorKeepTime(); return; }
  updatePreview();
}
function renderEditorKeepTime() {
  const t = $('#pv .pv-fg')?.currentTime || ED.style.trim_start;
  const scroll = $('.ed-side')?.scrollTop || 0;
  renderEditor();
  const fg = $('#pv .pv-fg');
  fg.addEventListener('loadedmetadata', () => { fg.currentTime = t; }, { once: true });
  $('.ed-side').scrollTop = scroll;
}
function editorProgress(d) {
  const box = $('#edProg');
  if (!ED || d.id !== ED.id || !box) return;
  box.hidden = false;
  const pct = d.status === 'rendering' ? Math.round((d.progress || 0) * 100) : d.status === 'done' ? 100 : 0;
  box.querySelector('i').style.width = `${pct}%`;
  box.querySelector('span').textContent = d.status === 'rendering' ? `Rendering ${pct}%`
    : d.status === 'done' ? 'Done' : d.status === 'failed' ? `Failed: ${d.error}` : 'Preparing…';
  box.classList.toggle('bad', d.status === 'failed');
  $('.ed-actions .gold').disabled = d.status === 'rendering';
  if (d.status === 'done') { toast('Clip updated'); setTimeout(() => { if (ED?.id === d.id) closeModal(); }, 600); }
}

const FORM = [
  ['When to clip', [
    ['sensitivity', 'Sensitivity', 'range', { min: 1.5, max: 6, step: 0.1, hint: 'Lower = more clips. Higher = only the biggest moments.' }],
    ['spike_ratio', 'Minimum speed-up (×)', 'number', { step: 0.1 }],
    ['min_msgs_per_10s', 'Minimum chat activity (msgs / 10s)', 'number', {}],
    ['cooldown_s', 'Cooldown between clips (s)', 'number', {}],
    ['max_clips_per_hour', 'Most clips per hour, per streamer', 'number', {}],
  ]],
  ['Clip shape', [
    ['clip_min_s', 'Shortest clip (s)', 'number', {}],
    ['clip_max_s', 'Longest clip (s)', 'number', {}],
    ['pre_roll_s', 'Lead-in before chat reacts (s)', 'number', {}],
    ['post_roll_s', 'Tail after chat calms (s)', 'number', {}],
    ['layout_zoom', 'Gameplay zoom', 'number', { step: 0.05 }],
  ]],
  ['Recording', [
    ['buffer_minutes', 'Footage kept (minutes)', 'number', {}],
    ['quality', 'Stream quality preference', 'text', {}],
    ['skip_reruns', 'Ignore reruns', 'checkbox', {}],
  ]],
  ['Captions and titles', [
    ['whisper_model', 'Speech model', 'select', { options: ['tiny', 'base', 'small', 'medium'], hint: 'Bigger is more accurate but slower.' }],
    ['captions_uppercase', 'UPPERCASE captions', 'checkbox', {}],
    ['llm_titles', 'Let Claude write titles (needs an API key in .env)', 'checkbox', {}],
    ['llm_model', 'Claude model', 'text', {}],
  ]],
];
function settingsPanel() {
  const v = S.settings;
  const field = ([key, label, type, o]) => {
    const val = v[key];
    if (type === 'checkbox') return `<div class="field check"><label><input type="checkbox" name="${key}" ${val ? 'checked' : ''}>${label}</label></div>`;
    let input;
    if (type === 'range') input = `<input type="range" name="${key}" min="${o.min}" max="${o.max}" step="${o.step}" value="${val}" oninput="this.closest('.field').querySelector('output').textContent=this.value">`;
    else if (type === 'select') input = `<select name="${key}">${o.options.map((x) => `<option ${x === val ? 'selected' : ''}>${x}</option>`).join('')}</select>`;
    else input = `<input type="${type}" name="${key}" value="${esc(val)}" ${o.step ? `step="${o.step}"` : ''}>`;
    return `<div class="field"><label>${label}${type === 'range' ? `<output>${val}</output>` : ''}</label>${input}${o.hint ? `<small>${o.hint}</small>` : ''}</div>`;
  };
  return `${head('House rules', 'Settings')}
    <div class="note">Titles: <b>${S.llm.mode === 'claude' ? 'Claude' : 'templates'}</b> — ${esc(S.llm.note || '')}</div>
    <div class="note">Twitch schedules: <b>${S.twitchApi ? 'connected' : 'learned from past streams'}</b></div>
    <form id="settingsForm">${FORM.map(([t, fields]) => `<div class="sub">${t}</div>${fields.map(field).join('')}`).join('')}
      <button class="gold save" type="submit">Save</button></form>`;
}

/* ═══════════════════════════ live data ═══════════════════════════ */
function chatEnergy() {
  let best = 0;
  for (const c of Object.values(S.chat)) best = Math.max(best, c.rate / Math.max(c.base, 0.3));
  stations.get('chat').crew.rate = Math.min(3, best * 0.5);
}

function handle({ type, data }) {
  switch (type) {
    case 'snapshot':
      S.roles = data.roles; S.settings = data.settings; S.llm = data.llm; S.twitchApi = data.twitch_api;
      S.agents = new Map(data.agents.map((a) => [a.name, a]));
      S.streamers = new Map(data.streamers.map((s) => [s.id, s]));
      S.jobs = new Map(data.jobs.map((j) => [j.id, j]));
      S.clips = data.clips; S.logs = data.logs;
      refreshAll(); syncOrbs(); renderStats(); panelDirty = true;
      break;
    case 'agent': S.agents.set(data.name, data); refreshStation(data.role); renderStats(); markPanel('agent', data.role); break;
    case 'agent_gone': {
      const role = data.name.split(':')[0];
      S.agents.delete(data.name); refreshStation(role); renderStats(); markPanel('agent', role); break;
    }
    case 'streamer': {
      const prev = S.streamers.get(data.id);
      if (prev && !data.chat) data.chat = prev.chat;
      S.streamers.set(data.id, data); renderStats(); markPanel('streamers'); break;
    }
    case 'streamer_gone': S.streamers.delete(data.id); delete S.chat[data.id]; chatEnergy(); renderStats(); markPanel('streamers'); break;
    case 'chat': S.chat[data.id] = { rate: data.p[1], base: data.p[2] }; chatEnergy(); break;
    case 'job': S.jobs.set(data.id, data); syncOrbs(); renderStats(); break;
    case 'job_gone': S.jobs.delete(data.id); syncOrbs(); break;
    case 'clip': S.clips.unshift(data); updateVault(); renderStats(); markPanel('clips'); toast(`New clip: “${data.title}”`); break;
    case 'clip_gone': S.clips = S.clips.filter((c) => c.id !== data.id); updateVault(); renderStats(); markPanel('clips'); break;
    case 'clip_update': S.clips = S.clips.map((c) => (c.id === data.id ? data : c)); updateVault(); markPanel('clips'); break;
    case 'clip_edit':
      S.edits[data.id] = data;
      if (data.status === 'done' || data.status === 'failed' || data.status === 'ready') setTimeout(() => { delete S.edits[data.id]; markPanel('clips'); }, 1500);
      editorProgress(data); markPanel('clips');
      if (data.status === 'transcribing' && $('#edWait')) $('#edWait').textContent = 'Transcribing the speech for captions… (first time only)';
      if (data.status === 'preparing' && $('#edWait')) $('#edWait').textContent = 'Pulling the clean footage out of the clip…';
      break;
    case 'requirements': S.mchat.requirements = data; if (S.panel?.kind === 'chat') renderChat(); break;
    case 'log': {
      S.logs.push(data);
      if (S.logs.length > 400) S.logs.shift();
      markPanel('agent', data.agent.split(':')[0]);
      break;
    }
    case 'settings': S.settings = data; break;
    case 'llm': S.llm = data; break;
  }
}

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { S.connected = true; renderStats(); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => { S.connected = false; renderStats(); setTimeout(connect, 1500); };
}

/* ═══════════════════════════ clicks & forms ═══════════════════════════ */
const armed = new WeakSet();
function confirmTwice(btn, label) {
  if (armed.has(btn)) return true;
  armed.add(btn);
  const old = btn.textContent;
  btn.textContent = label;
  setTimeout(() => { armed.delete(btn); if (btn.isConnected) btn.textContent = old; }, 3000);
  return false;
}

const setMenu = (open) => {
  $('#menu').classList.toggle('open', open);
  $('#menu').setAttribute('aria-hidden', String(!open));
  $('.burger').classList.toggle('open', open);
  $('.burger').setAttribute('aria-expanded', String(open));
};

document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-act]');
  if (el?.dataset.act === 'menu') { setMenu(!$('#menu').classList.contains('open')); return; }
  if (!e.target.closest('#menu') || el) setMenu(false);
  if (!el) {
    if (e.target.id === 'modal') closeModal();
    return;
  }
  const { act, id } = el.dataset;
  try {
    switch (act) {
      case 'streamers': case 'clips': case 'settings':
        if (S.panel?.kind === act) closePanel(); else { openPanel(act); if (act === 'clips') focusStation(vault.st); else goHome(); }
        break;
      case 'close': closePanel(); break;
      case 'closemodal': closeModal(); break;
      case 'play': openClip(id); break;
      case 'edit': openEditor(id); break;
      case 'chat-new': setChat(await api('POST', '/api/manager/chat/start')); break;
      case 'chat-end': setChat(await api('POST', '/api/manager/chat/end')); break;
      case 'chat-status': openPanel('agent', 'manager'); break;
      case 'chat-reqs': S.mchat.showReqs = !S.mchat.showReqs; renderChat(); break;
      case 'req-del': await api('DELETE', `/api/manager/requirements/${encodeURIComponent(id)}`); break;
      case 'ed-play': { const fg = $('#pv .pv-fg'); if (fg.paused) fg.play(); else fg.pause(); break; }
      case 'ed-scheme': { const { name, ...s } = SCHEMES[Number(id)]; Object.assign(ED.style, s); renderEditorKeepTime(); break; }
      case 'ed-bg': ED.style.background = id; renderEditorKeepTime(); break;
      case 'ed-render': {
        $('#pv .pv-fg')?.pause();
        const res = await api('POST', `/api/clips/${encodeURIComponent(ED.id)}/render`, ED.style);
        ED.style = { ...ED.style, ...res.style };
        editorProgress({ id: ED.id, status: 'rendering', progress: 0 });
        break;
      }
      case 'copy': await navigator.clipboard.writeText(el.dataset.text); toast('Copied'); break;
      case 'clip': await api('POST', `/api/streamers/${id}/clip`); toast('Clipping the last minute…'); break;
      case 'toggle': await api('POST', `/api/streamers/${id}/toggle`); break;
      case 'remove': if (confirmTwice(el, 'Sure?')) await api('DELETE', `/api/streamers/${id}`); break;
      case 'restart': await api('POST', `/api/agents/${encodeURIComponent(el.dataset.name)}/restart`); toast('Getting them back on their feet…'); break;
      case 'delclip': if (confirmTwice(el, 'Delete for good?')) { closeModal(); await api('DELETE', `/api/clips/${encodeURIComponent(id)}`); toast('Clip deleted'); } break;
    }
  } catch (err) { toast(err.message, true); }
});

document.addEventListener('input', (e) => { if (ED && e.target.closest('.editor') && e.target.dataset.k) editorInput(e.target); });
document.addEventListener('change', (e) => { if (ED && e.target.closest('.editor') && e.target.dataset.k && e.target.tagName === 'SELECT') editorInput(e.target); });
document.addEventListener('input', (e) => {
  if (e.target.id === 'edSeek' && ED) { const fg = $('#pv .pv-fg'); fg.currentTime = Number(e.target.value); }
});
document.addEventListener('keydown', (e) => {
  if (e.target.id === 'chatText' && e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

document.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (e.target.id === 'chatForm') { sendChat(); return; }
  try {
    if (e.target.id === 'addForm') {
      const channel = $('#addInput').value.trim();
      if (!channel) return;
      const s = await api('POST', '/api/streamers', { channel, platform: $('#addPlatform').value });
      $('#addInput').value = '';
      toast(`${s.name} added — the crew is on it`);
    } else if (e.target.id === 'settingsForm') {
      const body = {};
      e.target.querySelectorAll('[name]').forEach((f) => { body[f.name] = f.type === 'checkbox' ? f.checked : f.value; });
      S.settings = await api('PUT', '/api/settings', body);
      toast('Settings saved');
      closePanel();
    }
  } catch (err) { toast(err.message, true); }
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if ($('#menu').classList.contains('open')) return setMenu(false);
  if ($('#modal').classList.contains('show')) closeModal(); else closePanel();
});
setInterval(() => { if (S.panel?.kind === 'streamers') panelDirty = true; }, 30000);
setTimeout(hideHint, 12000);

refreshAll();
renderStats();
connect();
requestAnimationFrame(frame);
