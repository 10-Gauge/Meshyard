import { createViewer } from "/static/viewer.js";

const $ = (id) => document.getElementById(id);
const state = { channel: null, category: "", q: "", offset: 0, items: [], selected: null, printDefaults: {} };

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), data);
  return data;
}
const qs = (o) => new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v != null))).toString();

// ---------- 3D viewer ----------
function viewerMsg(text, corner = false) {
  const m = $("viewer-msg");
  m.textContent = text;
  m.style.placeItems = corner ? "end start" : "center";
}
const viewer = createViewer($("viewer"), { onMessage: viewerMsg });
const showModel = (url) => viewer.show(url);

// ---------- jobs ----------
function trackJob(id, onDone, onError) {
  const el = document.createElement("div");
  el.className = "job";
  $("jobs").prepend(el);
  const tick = async () => {
    let j;
    try { j = await api(`/api/jobs/${id}`); } catch (e) { el.textContent = e.message; return; }
    el.className = "job " + j.state;
    el.innerHTML = `<b></b> <span class="muted"></span><pre></pre>`;
    el.querySelector("b").textContent = j.label;
    el.querySelector("span").textContent = j.state === "running" ? "working…" : j.state === "done" ? "done" : "failed";
    el.querySelector("pre").textContent = (j.error ? j.error + "\n" : "") + j.log.slice(-6).join("\n");
    if (j.state === "running") return setTimeout(tick, 1000);
    if (j.state === "done") { onDone && onDone(j.result); setTimeout(() => el.remove(), 6000); }
    else { el.onclick = () => el.remove(); onError && onError(j.error || "failed"); }
  };
  tick();
}

// ---------- installs ----------
async function loadInstalls() {
  const d = await api("/api/installs");
  state.printDefaults = d.print;
  const sel = $("channel");
  sel.innerHTML = "";
  for (const i of d.installs) {
    const o = document.createElement("option");
    o.value = i.channel;
    o.textContent = `${i.channel} ${i.version}${i.indexed ? "" : " (not indexed)"}`;
    sel.append(o);
  }
  const saved = localStorageGet("channel");
  const pick = d.installs.find((i) => i.channel === saved) || d.installs.find((i) => i.channel === d.default_channel) || d.installs[0];
  showToolState(d.tools);
  if (!pick) {
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = "Choose game folder…";
    btn.onclick = chooseGameFolder;
    showEmpty("No Star Citizen install found (no LIVE or PTU folder containing Data.p4k).\n" +
      "Choose your StarCitizen folder: the one that contains LIVE (and PTU, if you have it)." +
      (d.searched.length ? "\n\nLooked in: " + d.searched.join(", ") : ""), [btn]);
    return;
  }
  sel.value = pick.channel;
  state.channel = pick.channel;
  state.installs = d.installs;
  applyPrintDefaults(d.print);
  api("/api/logos?" + qs({ channel: state.channel })).then((l) => {
    const sel = $("p-logo");
    for (const it of l.items) if (![...sel.options].some((o) => o.value === it.code)) sel.append(new Option(it.name, it.code));
  }).catch(() => {});
  fillExportFormats(d.export_formats || [], !!d.tools.blender);
  showBuild();
  search(true);
}
function showToolState(tools) {
  const missing = Object.entries(tools).filter(([k, v]) => !v && k !== "blender").map(([k]) => k);
  $("toolwarn").textContent = missing.length ? `Missing: ${missing.join(", ")}` : "";
  $("tools-dl").hidden = !missing.length;
  $("b-blender").disabled = !tools.blender;
  $("b-blender").title = tools.blender ? "Install the Blender add-ons and open the sandbox scene"
    : "Blender isn't installed (get it from blender.org), then restart this app";
}
async function chooseGameFolder() {
  const path = prompt("Path to your StarCitizen folder (the one that contains LIVE):");
  if (!path) return;
  try {
    await api("/api/roots", { path });
    await loadInstalls();
  } catch (e) { alert(e.message); }
}
let toolsJob = null;
async function downloadTools() {
  if (toolsJob) return;
  $("tools-dl").disabled = true;
  try {
    const { job } = await api("/api/tools/update", {});
    toolsJob = job;
    trackJob(job, () => { toolsJob = null; $("tools-dl").disabled = false; loadInstalls(); },
      (err) => { toolsJob = null; $("tools-dl").disabled = false; alert("Downloading the tools failed: " + err); });
  } catch (e) { $("tools-dl").disabled = false; alert(e.message); }
}
$("tools-dl").onclick = downloadTools;
$("b-blender").onclick = async () => {
  try { const { job } = await api("/api/blender", { action: "open" }); trackJob(job); }
  catch (e) { alert(e.message); }
};
function showBuild() {
  const i = state.installs.find((x) => x.channel === state.channel);
  $("build").textContent = i ? `build ${i.change} · ${i.path}` : "";
}
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

$("channel").onchange = (e) => { state.channel = e.target.value; localStorageSet("channel", state.channel); state.category = ""; showBuild(); search(true); };
$("reindex").onclick = async () => {
  try {
    const { job } = await api("/api/update", { channel: state.channel });
    trackJob(job, () => loadInstalls());
  } catch (e) { alert(e.message); }
};

// ---------- search ----------
let timer;
$("q").oninput = (e) => { clearTimeout(timer); timer = setTimeout(() => { state.q = e.target.value; search(true); }, 200); };
$("rawgeo").onchange = () => search(true);
$("more").onclick = () => search(false);

function showEmpty(text, actions = []) {
  $("results").innerHTML = "";
  $("count").textContent = "";
  $("empty").hidden = false;
  $("empty-text").textContent = text;
  $("empty-actions").replaceChildren(...actions);
}

async function search(reset) {
  if (!state.channel) return;
  if (reset) { state.offset = 0; state.items = []; }
  let d;
  try {
    d = await api("/api/assets?" + qs({ channel: state.channel, q: state.q, category: state.category,
      kind: $("rawgeo").checked ? "" : "entity", offset: state.offset, limit: 100 }));
  } catch (e) {
    const inst = (state.installs || []).find((i) => i.channel === state.channel);
    const idx = inst && inst.index;
    if (e.needs_index && idx && idx.state === "running") {
      showEmpty(`Indexing ${state.channel} ${inst.version}… the first time takes a few minutes. This page refreshes when it's done.`);
      setTimeout(() => loadInstalls(), 5000);
    } else if (e.needs_index && idx && idx.state === "error") {
      showEmpty(`Indexing ${state.channel} failed: ${idx.error}\n\nFix the cause, or click "Download tools" to update them, then click "Update index".`);
    } else {
      showEmpty(e.needs_index ? `${state.channel} isn't indexed for this build yet. Click "Update index" (takes a few minutes).` : e.message);
    }
    $("cats").innerHTML = "";
    return;
  }
  $("empty").hidden = true;
  state.items.push(...d.items);
  state.offset += d.items.length;
  $("count").textContent = `${d.total.toLocaleString()} assets`;
  $("more").hidden = state.offset >= d.total;
  renderCats(d.categories);
  const ul = $("results");
  if (reset) ul.innerHTML = "";
  for (const a of d.items) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="n"></div><div class="r"></div>`;
    li.querySelector(".n").textContent = a.name;
    li.querySelector(".r").textContent = [a.manufacturer, a.category, a.subcategory, a.record].filter(Boolean).join(" · ");
    li.onclick = () => select(a, li);
    ul.append(li);
  }
}

function renderCats(cats) {
  const nav = $("cats");
  nav.innerHTML = "";
  const all = [{ category: "", n: cats.reduce((s, c) => s + c.n, 0) }, ...cats];
  for (const c of all) {
    if (c.category === "geometry" && !$("rawgeo").checked) continue;
    const b = document.createElement("button");
    b.innerHTML = "<span></span><span class='muted'></span>";
    b.firstChild.textContent = c.category || "All";
    b.lastChild.textContent = c.n.toLocaleString();
    if (c.category === state.category) b.className = "on";
    b.onclick = () => { state.category = c.category; search(true); };
    nav.append(b);
  }
}

// ---------- detail / actions ----------
function select(a, li) {
  state.selected = a;
  document.querySelectorAll("#results li.on").forEach((x) => x.classList.remove("on"));
  li.classList.add("on");
  $("info").hidden = false;
  $("d-name").textContent = a.name;
  $("d-meta").textContent = [a.manufacturer, a.category, a.subcategory, a.record].filter(Boolean).join(" · ");
  $("d-desc").textContent = a.description || "";
  $("p-result").textContent = "";
  $("m-result").textContent = "";
  $("p-standtext").value = a.name;  // default plate text: the ship's name, editable
  state.thin = null;
  $("gear-row").hidden = a.category !== "spaceships";
  $("weapons-row").hidden = a.category !== "spaceships";
  $("vtol-row").hidden = a.category !== "spaceships";
  $("p-thin").hidden = true;
  $("p-thin").innerHTML = "";
  loadModel("preview");
}

async function loadModel(variant, spawn = false) {
  const a = state.selected;
  if (!a) return;
  if (!spawn) {
    state.variant = variant;
    viewer.clear();
    viewerMsg("Exporting model… big ships can take a minute or two the first time.");
  }
  try {
    const gear = gearUp() && variant !== "animated" ? "up" : "down";
    const weapons = weaponsOut() && variant !== "animated" ? "out" : "";
    const vtol = !!vtolOn() && variant !== "animated";
    const { job } = await api("/api/model", { channel: state.channel, id: a.id, variant, spawn, gear, weapons, vtol });
    trackJob(job, (r) => { if (!spawn && state.selected === a) showModel(r.url); },
      (err) => { if (!spawn && state.selected === a) viewerMsg("Export failed: " + err.split("\n").slice(-3).join(" ")); });
  } catch (e) { viewerMsg(e.message); }
}
const gearUp = () => $("b-gear").checked && !$("gear-row").hidden && state.selected && !state.selected.figure;
$("b-gear").onchange = () => loadModel(state.variant || "preview");
const weaponsOut = () => $("b-weapons").checked && !$("weapons-row").hidden && state.selected && !state.selected.figure;
$("b-weapons").onchange = () => loadModel(state.variant || "preview");
const vtolOn = () => $("b-vtol").checked && !$("vtol-row").hidden && state.selected && !state.selected.figure;
$("b-vtol").onchange = () => loadModel(state.variant || "preview");
$("b-preview").onclick = () => loadModel("preview");
$("b-full").onclick = () => loadModel("full");
$("b-spawn").onclick = () => loadModel("full", true);
$("b-spawn-anim").onclick = () => loadModel("animated", true);

// ---------- print export ----------
const PRINT_FIELDS = { size_mm: "p-size", voxel_mm: "p-voxel", nozzle_mm: "p-nozzle", min_feature_nozzles: "p-minn",
  stand_height_mm: "p-standh", tilt_deg: "p-tilt", hollow_mm: "p-hollow" };
function applyPrintDefaults(p) {
  for (const [k, id] of Object.entries(PRINT_FIELDS)) if (p[k] != null) $(id).value = p[k];
  $("p-mode").value = p.mode || "solid";
  $("p-format").value = p.format || "3mf";
  $("p-stand").value = p.stand || "none";
  $("p-standshape").value = p.stand_shape || "round";
  $("p-thicken").checked = p.thicken !== false;
  updateMinMm();
}
function updateMinMm() {
  const mm = (+$("p-nozzle").value || 0) * (+$("p-minn").value || 0);
  $("p-minmm").textContent = `= ${mm.toFixed(2)} mm`;
  const noStand = $("p-stand").value === "none";
  $("p-standh").disabled = noStand;
  $("p-standshape").disabled = noStand;
  for (const id of ["p-standtext", "p-standtext2", "p-logo"]) $(id).disabled = noStand || $("p-standshape").value !== "rect";
}
for (const id of ["p-nozzle", "p-minn", "p-stand", "p-standshape"]) $(id).addEventListener("input", updateMinMm);

function printOptions() {
  const o = { mode: $("p-mode").value, format: $("p-format").value, stand: $("p-stand").value, thicken: $("p-thicken").checked,
    stand_shape: $("p-standshape").value, stand_text: $("p-standtext").value.trim(),
    stand_text2: $("p-standtext2").value.trim(), stand_scale: $("p-standscale").checked, stand_logo: $("p-logo").value };
  for (const [k, id] of Object.entries(PRINT_FIELDS)) o[k] = +$(id).value;
  const overrides = {};
  for (const row of $("p-thin").querySelectorAll("tr[data-name]")) {
    const action = row.querySelector("select").value;
    if (action !== "auto") overrides[row.dataset.name] = { action, mm: +row.querySelector("input").value };
  }
  o.part_overrides = overrides;
  return o;
}

function showFiles(box, urls) {
  for (const href of urls) {
    const l = document.createElement("a");
    l.href = href; l.textContent = "Download " + decodeURIComponent(href.split("/").pop());
    box.append(document.createElement("br"), l);
  }
}

$("b-thin").onclick = async () => {
  const a = state.selected;
  const box = $("p-thin");
  box.hidden = false;
  box.textContent = "Measuring parts at print size…";
  try {
    const { job } = await api("/api/print/analyze", { channel: state.channel, ...target(a), options: printOptions() });
    trackJob(job, (r) => { if (state.selected === a) renderThin(r); }, (err) => { box.textContent = err; });
  } catch (e) { box.textContent = e.message; }
};
function renderThin(r) {
  const box = $("p-thin");
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "muted";
  head.textContent = `${r.size_mm.join(" × ")} mm at ${r.scale}. ` + (r.slender.length
    ? `Slender parts (orange = thinner than ${r.min_feature_mm} mm). Auto thickens them to the minimum; pick a bigger size, or remove a part to replace it with wire or rod.`
    : "No slender parts found.");
  box.append(head);
  if (!r.slender.length) return;
  const t = document.createElement("table");
  t.innerHTML = "<thead><tr><th>Part</th><th>Length</th><th>Thickness</th><th>Action</th><th>Size</th></tr></thead><tbody></tbody>";
  for (const p of r.slender) {
    const tr = document.createElement("tr");
    tr.dataset.name = p.name;
    tr.innerHTML = `<td class="n"></td><td></td><td></td><td><select><option value="auto">Auto</option>` +
      `<option value="thicken">Thicken to</option><option value="remove">Remove</option></select></td>` +
      `<td><input type="number" min="0.2" max="20" step="0.1"> mm</td>`;
    const [n, len, th] = tr.querySelectorAll("td");
    n.textContent = p.name; n.title = p.name;
    len.textContent = p.length_mm + " mm";
    th.textContent = p.thickness_mm + " mm";
    if (p.too_thin) th.className = "thin";
    const sel = tr.querySelector("select"), inp = tr.querySelector("input");
    inp.value = Math.max(r.min_feature_mm * 1.5, p.thickness_mm).toFixed(1);
    inp.disabled = true;
    sel.onchange = () => { inp.disabled = sel.value !== "thicken"; };
    t.tBodies[0].append(tr);
  }
  box.append(t);
}

$("b-print").onclick = async () => {
  const a = state.selected;
  const options = printOptions();
  $("p-result").textContent = "Working… large ships can take a few minutes.";
  try {
    const { job } = await api("/api/print", { channel: state.channel, ...target(a), options });
    trackJob(job, (r) => {
      const box = $("p-result");
      box.innerHTML = "";
      const s = document.createElement("div");
      const extras = [];
      if (r.thickened) extras.push(`${r.thickened} thin parts thickened to ${r.min_feature_mm} mm`);
      if (r.stand && r.stand !== "none") extras.push(r.stand === "peg" ? "stand with peg" : "stand attached");
      s.textContent = `${r.size_mm.map((x) => x.toFixed(0)).join(" × ")} mm (${r.scale}) · ${r.triangles.toLocaleString()} triangles · ` +
        (r.mode === "solid" ? (r.watertight ? "watertight" : "NOT watertight") + ` · ${r.volume_cm3} cm³ · detail ${r.voxel_mm} mm`
                            : `${r.watertight_bodies}/${r.bodies} parts watertight`) +
        (extras.length ? " · " + extras.join(" · ") : "");
      box.append(s);
      showFiles(box, r.downloads);
    }, (err) => { $("p-result").textContent = err.split("\n").slice(-3).join(" "); });
  } catch (e) { $("p-result").textContent = e.message; }
};

// ---------- model export ----------
function fillExportFormats(list, haveBlender) {
  const sel = $("m-format");
  const prev = sel.value || localStorageGet("exportFormat") || "glb";
  sel.innerHTML = "";
  for (const [f, label] of list) {
    const o = document.createElement("option");
    o.value = f;
    o.textContent = label + (label.includes("Blender") && !haveBlender ? " (set up Blender first)" : "");
    o.disabled = label.includes("Blender") && !haveBlender;
    sel.append(o);
  }
  sel.value = [...sel.options].some((o) => o.value === prev && !o.disabled) ? prev : "glb";
}
$("m-format").onchange = (e) => localStorageSet("exportFormat", e.target.value);
$("b-export").onclick = async () => {
  const a = state.selected;
  const box = $("m-result");
  box.textContent = "Exporting…";
  try {
    const { job } = await api("/api/export", { channel: state.channel, ...target(a), format: $("m-format").value, textures: $("m-tex").checked });
    trackJob(job, (r) => {
      box.innerHTML = "";
      const s = document.createElement("div");
      s.textContent = `${r.files.length} file${r.files.length > 1 ? "s" : ""}` + (r.files.length > 1 ? " (zipped for download)" : "");
      box.append(s);
      showFiles(box, [r.url]);
    }, (err) => { box.textContent = err.split("\n").slice(-3).join(" "); });
  } catch (e) { box.textContent = e.message; }
};

// ---------- character builder ----------
// The print / thin-part / export requests are about an indexed asset or a built figure.
function target(a) { return a.figure ? { figure: a.figure, label: a.name } : { id: a.id, gear: gearUp() ? "up" : "down", weapons: weaponsOut() ? "out" : "", vtol: !!vtolOn() }; }

const chars = { on: false, slots: [], picks: {}, loadedBody: null };

$("b-chars").onclick = async () => {
  chars.on = !chars.on;
  document.body.classList.toggle("chars", chars.on);
  for (const id of ["q", "rawgeo", "count", "results", "more"]) $(id).closest(".searchbar, #list > *").hidden = chars.on;
  $("charpanel").hidden = !chars.on;
  if (chars.on) {
    $("empty").hidden = true;
    if (chars.loadedBody !== $("c-body").value) await loadCharSlots();
    if (state.selected && state.selected.figure) showFigureInfo(state.selected);
  } else {
    search(true);
  }
};

async function loadCharSlots() {
  const body = $("c-body").value;
  $("c-status").textContent = "Loading…";
  try {
    const d = await api("/api/character/slots?" + qs({ channel: state.channel, body }));
    chars.slots = d.slots;
    chars.loadedBody = body;
    const head = $("c-head");
    head.innerHTML = '<option value="">(none)</option>';
    for (const h of d.heads) head.append(new Option(h.name, h.id));
    if (d.heads.length) head.selectedIndex = 1;
    const pose = $("c-pose");
    if (pose.options.length < 2) {
      for (const p of d.poses || []) pose.append(new Option(p.label, p.id));
      pose.value = "relaxed";
      fillWeapons("");
    }
    syncWeaponRow();
    const box = $("c-slots");
    box.innerHTML = "";
    for (const s of d.slots) {
      const row = document.createElement("div");
      row.className = "cslot";
      row.innerHTML = `<label></label><input type="search" placeholder="Filter…"><select><option value="">(none)</option></select>`;
      row.querySelector("label").textContent = s.label;
      const input = row.querySelector("input"), sel = row.querySelector("select");
      sel.onchange = () => { chars.picks[s.id] = sel.value; };
      let t;
      input.oninput = () => { clearTimeout(t); t = setTimeout(() => fillSlot(s.id, sel, input.value), 250); };
      box.append(row);
      fillSlot(s.id, sel, "");
    }
    $("c-status").textContent = "";
  } catch (e) { $("c-status").textContent = e.message; }
}

async function fillSlot(slot, sel, q) {
  const keep = chars.picks[slot] || "";
  try {
    const d = await api("/api/character/items?" + qs({ channel: state.channel, body: $("c-body").value, slot, q }));
    sel.innerHTML = '<option value="">(none)</option>';
    for (const it of d.items) sel.append(new Option(it.name, it.id));
    if ([...sel.options].some((o) => o.value === String(keep))) sel.value = keep;
  } catch (e) { sel.innerHTML = `<option value="">${e.message}</option>`; }
}

async function fillWeapons(q) {
  const sel = $("c-weapon"), keep = sel.value;
  try {
    const d = await api("/api/character/weapons?" + qs({ channel: state.channel, q }));
    sel.innerHTML = '<option value="">(none)</option>';
    for (const it of d.items) sel.append(new Option(it.name, it.id));
    if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  } catch (e) { sel.innerHTML = `<option value="">${e.message}</option>`; }
}

// a weapon only makes sense in a pose; the A-pose is the raw export
function syncWeaponRow() {
  const off = !$("c-pose").value;
  $("c-wq").disabled = $("c-weapon").disabled = off;
  $("c-weaponrow").classList.toggle("muted", off);
}

$("c-pose").onchange = syncWeaponRow;
{
  let t;
  $("c-wq").oninput = () => { clearTimeout(t); t = setTimeout(() => fillWeapons($("c-wq").value), 250); };
}

$("c-body").onchange = () => { chars.picks = {}; loadCharSlots(); };
$("c-clear").onclick = () => {
  chars.picks = {};
  for (const sel of $("c-slots").querySelectorAll("select")) sel.value = "";
};

function figureName() {
  const names = [];
  for (const sel of $("c-slots").querySelectorAll("select")) if (sel.value) names.push(sel.selectedOptions[0].textContent);
  const core = names.find((n) => /core|armor|armour/i.test(n)) || names[0];
  return core ? core.replace(/\s+(Core|Arms|Legs|Helmet)\b.*$/i, "") : "Character";
}

function showFigureInfo(fig) {
  document.querySelectorAll("#results li.on").forEach((x) => x.classList.remove("on"));
  $("info").hidden = false;
  $("d-name").textContent = fig.name;
  $("d-meta").textContent = fig.meta;
  $("d-desc").textContent = "Built from your selections. Print settings below apply to the figure; size is its height.";
  for (const id of ["b-preview", "b-full", "b-spawn", "b-spawn-anim"]) $(id).hidden = true;
}

$("c-build").onclick = async () => {
  const picks = {};
  for (const [k, v] of Object.entries(chars.picks)) if (v) picks[k] = +v;
  const body = $("c-body").value;
  $("c-status").textContent = "Building… the first time each piece is exported takes a few seconds.";
  viewer.clear();
  viewerMsg("Assembling the character…");
  try {
    const pose = $("c-pose").value, weapon = pose ? $("c-weapon").value : "";
    const { job } = await api("/api/character/build", {
      channel: state.channel, body, picks, head: $("c-head").value, pose, weapon: weapon ? +weapon : null,
    });
    trackJob(job, (r) => {
      const name = figureName();
      const picked = [...$("c-slots").querySelectorAll("select")].filter((s) => s.value).map((s) => s.selectedOptions[0].textContent);
      if (pose) picked.push($("c-pose").selectedOptions[0].textContent);
      if (weapon) picked.push($("c-weapon").selectedOptions[0].textContent);
      state.selected = { figure: r.figure, name, meta: `${body} · ${picked.join(" · ") || "no armor"}` };
      showFigureInfo(state.selected);
      $("p-result").textContent = "";
      $("m-result").textContent = "";
      $("p-standtext").value = name;
      // figures: size is the height; finer detail and a stand under the feet suit them
      if (+$("p-size").value > 120) $("p-size").value = 75;
      if (+$("p-voxel").value > 0.12) $("p-voxel").value = 0.1;
      if ($("p-stand").value === "none") $("p-stand").value = "attached";
      updateMinMm();
      $("c-status").textContent = "";
      showModel(r.url);
    }, (err) => { $("c-status").textContent = err.split("\n").slice(-3).join(" "); viewerMsg("Build failed"); });
  } catch (e) { $("c-status").textContent = e.message; }
};

// leaving the builder by picking an asset restores the asset buttons
const _select = select;
select = function (a, li) {
  for (const id of ["b-preview", "b-full", "b-spawn", "b-spawn-anim"]) $(id).hidden = false;
  return _select(a, li);
};

loadInstalls().catch((e) => showEmpty("Could not reach the toolkit: " + e.message));
