// The 3D model viewer, shared by the browser page (app.js) and the desktop app's
// viewer pane (viewer.html).
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export function createViewer(el, { onMessage } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  // past 2x a 4K screen pays four times the pixels for no visible gain
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  // film-style tone mapping: bright panels roll off instead of clipping to white
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.85;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  // nothing in the scene moves: work the shadows out once per model, not every frame
  renderer.shadowMap.autoUpdate = false;
  el.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  // a soft studio around the model for reflections: without one, metal and glass have
  // nothing to reflect and render flat grey or black
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(studio(), 0.04).texture;
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 100000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 0.5));
  const sun = new THREE.DirectionalLight(0xfff4e8, 2.4);
  sun.position.set(3, 5, 4);
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.bias = -0.0005;
  sun.shadow.normalBias = 0.02;
  scene.add(sun, sun.target);
  // the shadow the model casts on the floor; no floor of its own
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShadowMaterial({ opacity: 0.28 }));
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  floor.visible = false;
  scene.add(floor);
  let gridColors = [0x888888, 0x555555];
  let grid = new THREE.GridHelper(10, 10, ...gridColors);
  scene.add(grid);
  let current = null, gridSize = 10, gridDivs = 10, home = null, loadSeq = 0;
  camera.position.set(7, 4.5, 8.5);  // until a model frames the view
  controls.target.set(0, 0.5, 0);

  function resize() {
    const w = el.clientWidth, h = el.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(() => { resize(); redraw(); }).observe(el);
  // Draw only when something changed (the camera moving, a model arriving): a still
  // view costs nothing, and the app stays responsive while it sits there.
  let dirty = true;
  const redraw = () => { dirty = true; };
  controls.addEventListener("change", redraw);
  (function loop() {
    requestAnimationFrame(loop);
    if (controls.update()) dirty = true;  // still easing after a drag
    if (!dirty) return;
    dirty = false;
    renderer.render(scene, camera);
  })();

  // Which graphics card draws the view. Edge and WebView2 drop to software drawing when
  // they distrust the driver, which makes the viewer crawl on any card: say so under the
  // model's size.
  const gpu = gpuName(renderer);
  const gpuNote = /swiftshader|basic render|llvmpipe|software/i.test(gpu)
    ? `\nDrawing without the graphics card (${gpu}), so the view will be slow. Updating the graphics driver usually fixes this.`
    : "";
  const message = (text, corner = false) => onMessage && onMessage(corner ? text + gpuNote : text, corner);

  function setGrid(size, divs) {
    scene.remove(grid);
    gridSize = size; gridDivs = divs;
    grid = new THREE.GridHelper(size, divs, ...gridColors);
    scene.add(grid);
    redraw();
  }

  function clear() {
    loadSeq++;
    if (current) { scene.remove(current); current = null; }
    redraw();
  }

  function resetView() {
    if (!home) return;
    camera.position.copy(home.pos);
    controls.target.copy(home.target);
    redraw();
  }

  function show(url) {
    const seq = ++loadSeq;
    message("Loading model…");
    new GLTFLoader().load(url, (gltf) => {
      if (seq !== loadSeq) return;  // a newer request replaced this one
      if (current) scene.remove(current);
      current = gltf.scene;
      // Ships carry their in-game light fixtures (77 on a Gladius, 5000+ on an Idris). three.js
      // compiles every light into every material's shader, which stalls or fails and leaves only
      // the grid on screen, so drop them and rely on the viewer's own lighting.
      const lights = [];
      let meshes = 0;
      // COLOR_0 holds the game's tint/wear masks, not paint; drawn as color it turns ships purple.
      // Game meshes are full of one-sided panels (the Alpha Wolf's nozzle petals face inward):
      // seen from behind they vanish, so draw both sides.
      current.traverse((o) => {
        if (o.isLight) lights.push(o);
        if (o.isMesh) meshes++;
        // light flare cards ("aegs_gladius_wingfinleft_decalglow"): in the game they're soft
        // glows around the lamps; here they're opaque slivers, some floating off the hull
        if (o.isMesh && (FLARE.test(o.name) || [].concat(o.material).every(blank))) o.visible = false;
        for (const m of o.isMesh ? [].concat(o.material) : []) {
          if (m.vertexColors) { m.vertexColors = false; m.needsUpdate = true; }
          if (m.side !== THREE.DoubleSide) { m.side = THREE.DoubleSide; m.needsUpdate = true; }
          // see-through two-sided parts are otherwise drawn twice (back faces, then front)
          if (m.transparent) m.forceSinglePass = true;
          tune(m);
          // a map on the second uv set of a part that has none fails to compile
          const g = o.geometry;
          if (!g.attributes.uv1 && g.attributes.uv && ["aoMap", "lightMap", "map", "normalMap", "emissiveMap"].some((k) => m[k] && m[k].channel === 1))
            g.setAttribute("uv1", g.attributes.uv);
        }
      });
      for (const l of lights) l.removeFromParent();
      current.updateMatrixWorld(true);
      // measured part by part, before the parts are merged, so stray bits can be left out
      const box = tightBox(current);
      if (box.isEmpty()) { message("The exported model has no visible geometry."); return; }
      // A ship is thousands of separate parts (26,000 on a Carrack), one draw call each:
      // big ships crawled. Parts that share a material become one mesh.
      if (meshes > MERGE_MIN_MESHES) current = mergeByMaterial(current);
      current.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
      scene.add(current);
      const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
      const r = Math.max(size.x, size.y, size.z) || 1;
      current.position.sub(new THREE.Vector3(center.x, box.min.y, center.z));
      const step = Math.pow(10, Math.floor(Math.log10(r)));
      setGrid(Math.ceil(r * 1.5 / step) * step, Math.ceil(r * 1.5 / step));
      camera.position.set(r * 1.1, r * 0.7, r * 1.3);
      controls.target.set(0, size.y / 2, 0);
      home = { pos: camera.position.clone(), target: controls.target.clone() };
      camera.near = r / 1000; camera.far = r * 100; camera.updateProjectionMatrix();
      // sun and its shadow box sized to the model (it sits on the floor at y = 0)
      sun.position.set(r * 0.6, r * 1.6, r * 0.9);
      sun.target.position.set(0, size.y / 2, 0);
      Object.assign(sun.shadow.camera, { left: -r, right: r, top: r, bottom: -r, near: r * 0.1, far: r * 4 });
      sun.shadow.camera.updateProjectionMatrix();
      sun.castShadow = true;
      floor.scale.set(r * 3, r * 3, 1);
      floor.visible = true;
      renderer.shadowMap.needsUpdate = true;
      redraw();
      message(`${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} m`, true);
    }, (p) => { if (seq === loadSeq && p.total) message(`Downloading model… ${Math.round(100 * p.loaded / p.total)}%`); },
    (err) => { if (seq === loadSeq) message("Could not load model: " + (err.message || err)); });
  }

  function setTheme(dark) {
    gridColors = dark ? [0x8a8f98, 0x4a4f58] : [0x9aa0a8, 0xcfd3d8];
    setGrid(gridSize, gridDivs);
  }

  renderer.domElement.addEventListener("dblclick", resetView);  return { show, clear, resetView, setTheme, message };
}

const MERGE_MIN_MESHES = 300;

function gpuName(renderer) {
  try {
    const gl = renderer.getContext(), ext = gl.getExtension("WEBGL_debug_renderer_info");
    return String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
  } catch { return ""; }
}

// One mesh per material, in world space. The game's parts share huge vertex buffers
// (see tightGeometryBounds), so copy only the vertices each part's triangles use.
// Skinned and instanced meshes are kept as they are.
function mergeByMaterial(root) {
  const out = new THREE.Group();
  const groups = new Map();
  const keep = [];
  root.traverse((o) => {
    if (!o.isMesh || !o.visible) return;
    if (o.isSkinnedMesh || o.isInstancedMesh || Array.isArray(o.material) || !o.geometry.attributes.position) { keep.push(o); return; }
    if (!groups.has(o.material)) groups.set(o.material, []);
    groups.get(o.material).push(o);
  });
  const NAMES = ["position", "normal", "uv", "uv1", "tangent"];
  let mark = new Int32Array(0), stamp = new Int32Array(0), pass = 0;
  const v = new THREE.Vector3(), nm = new THREE.Matrix3();
  for (const [material, parts] of groups) {
    if (parts.length === 1) { keep.push(parts[0]); continue; }
    // every attribute any part has (the material may read a second uv set for its
    // occlusion map); parts without one get zeros
    const size = {};
    for (const p of parts) for (const n of NAMES) { const a = p.geometry.attributes[n]; if (a && !size[n]) size[n] = a.itemSize; }
    const names = NAMES.filter((n) => size[n]);
    // pass 1: which vertices each part uses
    const plans = [];
    let nv = 0, ni = 0;
    for (const p of parts) {
      const g = p.geometry, count = g.attributes.position.count;
      if (mark.length < count) { mark = new Int32Array(count); stamp = new Int32Array(count); }
      pass++;
      const src = g.index ? g.index.array : null, n = g.index ? g.index.count : count;
      const used = [];
      for (let i = 0; i < n; i++) {
        const k = src ? src[i] : i;
        if (stamp[k] !== pass) { stamp[k] = pass; mark[k] = used.length; used.push(k); }
      }
      plans.push({ p, used, n, src });
      nv += used.length; ni += n;
    }
    const arrays = Object.fromEntries(names.map((n) => [n, new Float32Array(nv * size[n])]));
    const index = nv > 65535 ? new Uint32Array(ni) : new Uint16Array(ni);
    let vo = 0, io = 0;
    // pass 2: copy, moved into world space
    for (const { p, used, n, src } of plans) {
      const g = p.geometry, w = p.matrixWorld;
      nm.getNormalMatrix(w);
      pass++;
      for (let j = 0; j < used.length; j++) { stamp[used[j]] = pass; mark[used[j]] = j; }
      for (let i = 0; i < n; i++) index[io + i] = vo + mark[src ? src[i] : i];
      for (const name of names) {
        const a = g.attributes[name], dst = arrays[name], s = size[name];
        if (!a || a.itemSize !== s) continue;  // a part without uvs: zeros
        for (let j = 0; j < used.length; j++) {
          const k = used[j], o = (vo + j) * s;
          if (name === "position") { v.fromBufferAttribute(a, k).applyMatrix4(w); dst[o] = v.x; dst[o + 1] = v.y; dst[o + 2] = v.z; }
          else if (name === "normal") { v.fromBufferAttribute(a, k).applyMatrix3(nm).normalize(); dst[o] = v.x; dst[o + 1] = v.y; dst[o + 2] = v.z; }
          else if (name === "tangent") { v.fromBufferAttribute(a, k).transformDirection(w); dst[o] = v.x; dst[o + 1] = v.y; dst[o + 2] = v.z; dst[o + 3] = a.getW(k); }
          else { for (let c = 0; c < s; c++) dst[o + c] = a.getComponent(k, c); }
        }
      }
      vo += used.length; io += n;
    }
    const geo = new THREE.BufferGeometry();
    for (const name of names) geo.setAttribute(name, new THREE.BufferAttribute(arrays[name], size[name]));
    geo.setIndex(new THREE.BufferAttribute(index, 1));
    geo.computeBoundingBox(); geo.computeBoundingSphere();
    geo.userData.tight = geo.boundingBox.clone();
    const mesh = new THREE.Mesh(geo, material);
    mesh.name = material.name;
    out.add(mesh);
  }
  for (const o of keep) {  // moved over with their world transform
    const m = o.matrixWorld.clone();
    o.removeFromParent();
    o.matrixAutoUpdate = false; o.matrix.copy(m);
    out.add(o);
  }
  return out;
}
const FLARE = /decal_?glow|glow_?decal|headlights?_glow/i;

// The export marks every material non-metallic, so steel and chrome look like grey
// plastic. The game's own material notes come along (surface type, layer files): bare
// metal gets its shine back. Decals come through mirror-smooth; give them a satin finish.
// Decals the export can't fill in: stencils (painted logos and serials, which come out
// as plain white squares) and text the game draws at run time (the ship's name).
function blank(m) {
  const attrs = (m.userData.semantic && m.userData.semantic.authored_attributes) || [];
  const gen = (attrs.find((a) => a.name === "StringGenMask") || {}).value || "";
  return !m.userData.diffuse_tex && /%STENCIL_MAP|%USE_RTT_UV_FOR_DIFFUSE/.test(gen);
}

const RAW_METAL =/(^|[_/ ])(steel|chrome|aluminium|aluminum|copper|brass|titanium|bare_metal|raw_steel|metal_raw)/i;
function tune(m) {
  if (!m.isMeshStandardMaterial || m.userData.tuned) return;
  m.userData.tuned = true;
  const ud = m.userData;
  const attrs = (ud.semantic && ud.semantic.authored_attributes) || [];
  const attr = (k) => (attrs.find((a) => a.name === k) || {}).value || "";
  const shader = attr("Shader"), surface = attr("SurfaceType");
  const layers = (ud.layers || []).map((l) => l.path || "").join(" ");
  const painted = /paint|plastic|rubber|fabric|leather|tile/i.test(m.name + " " + layers);
  if (/decal/i.test(shader) || m.transparent) {
    m.roughness = Math.max(m.roughness, 0.45);
  } else if (!painted && (RAW_METAL.test(m.name) || /\/metal\//i.test(layers))) {
    m.metalness = 0.85;
    m.roughness = Math.min(Math.max(m.roughness, 0.25), 0.55);
  } else if (/^metal/i.test(surface) && !painted) {
    m.metalness = 0.35;
  }
  m.needsUpdate = true;
}

// A photo studio for reflections: a dim room with a big soft light overhead and two
// strip lights at the sides, blurred into an environment map once at startup.
function studio() {
  const room = new THREE.Scene();
  const box = new THREE.Mesh(new THREE.BoxGeometry(10, 10, 10),
    new THREE.MeshBasicMaterial({ color: 0x3a3d42, side: THREE.BackSide }));
  room.add(box);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(10, 10), new THREE.MeshBasicMaterial({ color: 0x1c1d20 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = -4.99;
  room.add(floor);
  const panel = (w, h, color, pos, look) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide }));
    m.position.set(...pos); m.lookAt(...look);
    room.add(m);
  };
  panel(6, 6, new THREE.Color(5, 5, 5), [0, 4.9, 0], [0, 0, 0]);         // overhead softbox
  panel(1.2, 6, new THREE.Color(3, 3, 3.2), [-4.9, 1, 1], [0, 1, 1]);    // left strip
  panel(1.2, 6, new THREE.Color(2.4, 2.3, 2.2), [4.9, 1, -1], [0, 1, -1]); // right strip
  panel(5, 1.5, new THREE.Color(1.5, 1.5, 1.6), [0, 2, -4.9], [0, 2, 0]);  // back rim
  return room;
}

// The game's meshes share one vertex buffer between many parts, and three.js sizes each
// part by the whole buffer: every hull panel of an Idris claims the whole hull's span, so
// the model was framed (and culled) as if it were twice its size. Measure only the
// vertices each part's triangles use, and use that for framing and frustum culling.
function tightGeometryBounds(geo) {
  if (geo.userData.tight !== undefined) return geo.userData.tight;
  const pos = geo.attributes.position, idx = geo.index;
  let tight = null;
  if (pos && idx) {
    const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    const a = idx.array, p = pos.array, st = pos.isInterleavedBufferAttribute ? pos.data.stride : 3,
      off = pos.isInterleavedBufferAttribute ? pos.offset : 0;
    if (!pos.normalized) {
      for (let i = 0; i < idx.count; i++) {
        const k = a[i] * st + off;
        for (let c = 0; c < 3; c++) { const v = p[k + c]; if (v < lo[c]) lo[c] = v; if (v > hi[c]) hi[c] = v; }
      }
      if (lo[0] <= hi[0]) {
        tight = new THREE.Box3(new THREE.Vector3(...lo), new THREE.Vector3(...hi));
        geo.boundingBox = tight.clone();
        geo.boundingSphere = tight.getBoundingSphere(new THREE.Sphere());
      }
    }
  }
  geo.userData.tight = tight;
  return tight;
}

// The model's box for framing. Exports can carry a stray part far off the hull (a
// Carrack interior panel 100 m to the side), which framed the ship as a speck: parts
// whose middle is far beyond where nearly all the others sit are left out.
function tightBox(root) {
  const boxes = [];
  root.traverse((o) => {
    if (!o.isMesh || !o.geometry || !o.visible) return;
    const t = o.isSkinnedMesh ? null : tightGeometryBounds(o.geometry);
    boxes.push(t ? t.clone().applyMatrix4(o.matrixWorld) : new THREE.Box3().setFromObject(o));
  });
  const box = new THREE.Box3();
  if (!boxes.length) return box;
  const centers = boxes.map((b) => b.getCenter(new THREE.Vector3()));
  const mid = (k) => centers.map((c) => c[k]).sort((a, b) => a - b)[centers.length >> 1];
  const m = new THREE.Vector3(mid("x"), mid("y"), mid("z"));
  const dist = centers.map((c) => c.distanceTo(m));
  const limit = strayLimit(dist);
  boxes.forEach((b, i) => { if (dist[i] <= limit) box.union(b); });
  return box;
}

// How far from the model's middle a part can sit and still belong to it: stray parts
// are a few odd ones beyond a clear gap. A long ship's engines (the Javelin's, 200 m
// back) run on continuously from the hull and stay in.
function strayLimit(dist) {
  if (dist.length < 20) return Infinity;
  const d = dist.slice().sort((a, b) => a - b);
  const gap = Math.max(2, 0.25 * d[Math.floor(d.length * 0.9)]);
  for (let i = Math.floor(d.length * 0.9); i < d.length; i++)
    if (d[i] - d[i - 1] > gap) return d[i - 1] + 1e-6;
  return Infinity;
}
