// The 3D model viewer, shared by the browser page (app.js) and the desktop app's
// viewer pane (viewer.html).
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export function createViewer(el, { onMessage } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  el.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 100000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.position.set(3, 5, 4);
  scene.add(sun);
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
  new ResizeObserver(resize).observe(el);
  (function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();

  const message = (text, corner = false) => onMessage && onMessage(text, corner);

  function setGrid(size, divs) {
    scene.remove(grid);
    gridSize = size; gridDivs = divs;
    grid = new THREE.GridHelper(size, divs, ...gridColors);
    scene.add(grid);
  }

  function clear() {
    loadSeq++;
    if (current) { scene.remove(current); current = null; }
  }

  function resetView() {
    if (!home) return;
    camera.position.copy(home.pos);
    controls.target.copy(home.target);
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
      // COLOR_0 holds the game's tint/wear masks, not paint; drawn as color it turns ships purple.
      // Game meshes are full of one-sided panels (the Alpha Wolf's nozzle petals face inward):
      // seen from behind they vanish, so draw both sides.
      current.traverse((o) => {
        if (o.isLight) lights.push(o);
        for (const m of o.isMesh ? [].concat(o.material) : []) {
          if (m.vertexColors) { m.vertexColors = false; m.needsUpdate = true; }
          if (m.side !== THREE.DoubleSide) { m.side = THREE.DoubleSide; m.needsUpdate = true; }
        }
      });
      for (const l of lights) l.removeFromParent();
      scene.add(current);
      current.updateMatrixWorld(true);
      const box = tightBox(current);
      if (box.isEmpty()) { message("The exported model has no visible geometry."); return; }
      const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
      const r = Math.max(size.x, size.y, size.z) || 1;
      current.position.sub(new THREE.Vector3(center.x, box.min.y, center.z));
      const step = Math.pow(10, Math.floor(Math.log10(r)));
      setGrid(Math.ceil(r * 1.5 / step) * step, Math.ceil(r * 1.5 / step));
      camera.position.set(r * 1.1, r * 0.7, r * 1.3);
      controls.target.set(0, size.y / 2, 0);
      home = { pos: camera.position.clone(), target: controls.target.clone() };
      camera.near = r / 1000; camera.far = r * 100; camera.updateProjectionMatrix();
      message(`${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} m`, true);
    }, (p) => { if (seq === loadSeq && p.total) message(`Downloading model… ${Math.round(100 * p.loaded / p.total)}%`); },
    (err) => { if (seq === loadSeq) message("Could not load model: " + (err.message || err)); });
  }

  function setTheme(dark) {
    gridColors = dark ? [0x8a8f98, 0x4a4f58] : [0x9aa0a8, 0xcfd3d8];
    setGrid(gridSize, gridDivs);
  }

  renderer.domElement.addEventListener("dblclick", resetView);
  return { show, clear, resetView, setTheme, message };
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

function tightBox(root) {
  const box = new THREE.Box3(), part = new THREE.Box3();
  root.traverse((o) => {
    if (!o.isMesh || !o.geometry) return;
    const t = o.isSkinnedMesh ? null : tightGeometryBounds(o.geometry);
    if (t) box.union(part.copy(t).applyMatrix4(o.matrixWorld));
    else box.union(part.setFromObject(o));
  });
  return box;
}
