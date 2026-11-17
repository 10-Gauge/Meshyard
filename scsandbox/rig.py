"""Pose a character figure: skeleton, skin weights, two-bone IK and the weapon.

Geometry comes from StarBreaker's skin export (correct positions, no skeleton) and
the skeleton + per-vertex weights from cgf-converter's export of the same .skin
(same vertex order; its positions are offset, but its skeleton is in StarBreaker's
frame). The figure faces -Z with Y up; left is -X.

A pose is a set of world-space deltas D[joint] (4x4) applied to each joint's
subtree; vertices are moved by linear blend skinning: v' = sum_k w_k * D[j_k] v.
"""

from __future__ import annotations

import json
import math
import struct

import numpy as np

COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

POSES = {
    "relaxed": "Standing, arms at sides (weapon on the back)",
    "aim_stand": "Standing, aiming",
    "aim_kneel": "Kneeling, aiming",
    "ready_stand": "Standing, low ready",
    "ready_kneel": "Kneeling, low ready",
}
KNEEL_DROP = 0.47  # how far the hips come down when kneeling (m)
# How the game handles each weapon (its record's equipCategory), and so how it's held:
# stocked = rifles, SMGs, shotguns, snipers, LMGs, grenade launcher, crossbow (stock in
# the shoulder); pistol (two hands, arms out); shouldered = railgun and rocket launcher
# (tube on top of the shoulder); heavy = the HMG (carried at the hip).
KINDS = ("stocked", "pistol", "shouldered", "heavy")


# --- glTF reading ---------------------------------------------------------------

class Gltf:
    def __init__(self, path):
        raw = open(path, "rb").read()
        n = struct.unpack("<I", raw[12:16])[0]
        self.j = json.loads(raw[20:20 + n])
        self.raw, self.off = raw, 20 + n + 8

    def acc(self, i):
        a = self.j["accessors"][i]
        bv = self.j["bufferViews"][a["bufferView"]]
        dt, nc = COMP[a["componentType"]], NCOMP[a["type"]]
        start = self.off + bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        item = np.dtype(dt).itemsize * nc
        stride = bv.get("byteStride", 0)
        if stride and stride != item:
            buf = np.frombuffer(self.raw, np.uint8, count=stride * a["count"], offset=start)
            buf = buf.reshape(a["count"], stride)[:, :item]
            out = np.frombuffer(buf.tobytes(), dt).reshape(a["count"], nc)
        else:
            out = np.frombuffer(self.raw, dt, count=a["count"] * nc, offset=start).reshape(a["count"], nc)
        if a.get("normalized"):
            out = out.astype(np.float64) / np.iinfo(dt).max
        return out


def _local(nd):
    if "matrix" in nd:
        return np.array(nd["matrix"], float).reshape(4, 4).T
    T = np.eye(4)
    if "translation" in nd:
        T[:3, 3] = nd["translation"]
    if "rotation" in nd:
        x, y, z, w = nd["rotation"]
        T[:3, :3] = T[:3, :3] @ np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    if "scale" in nd:
        T = T @ np.diag([*nd["scale"], 1.0])
    return T


class Rig:
    """Skeleton (joint names, parents, rest world matrices) + per-vertex weights."""

    def __init__(self, path):
        g = Gltf(path)
        N = g.j["nodes"]
        parent = {c: i for i, nd in enumerate(N) for c in nd.get("children", [])}
        world = {}

        def W(i):
            if i not in world:
                world[i] = (W(parent[i]) if i in parent and parent[i] != i else np.eye(4)) @ _local(N[i])
            return world[i]

        mesh_node = next(i for i, nd in enumerate(N) if "mesh" in nd and "skin" in nd)
        sk = g.j["skins"][N[mesh_node]["skin"]]
        joints = sk["joints"]
        self.names = [N[k].get("name") or f"joint{k}" for k in joints]
        self.rest = {self.names[a]: W(k) for a, k in enumerate(joints)}
        idx = {k: a for a, k in enumerate(joints)}
        self.parent = {}
        for a, k in enumerate(joints):
            p = parent.get(k)
            while p is not None and p not in idx:
                p = parent.get(p)
            self.parent[self.names[a]] = self.names[idx[p]] if p is not None else None
        J, Wt = [], []
        for pr in g.j["meshes"][N[mesh_node]["mesh"]]["primitives"]:
            # one block per primitive even when they share a vertex buffer: the worn-mesh
            # exports repeat the vertices per primitive the same way, so counts match
            J.append(g.acc(pr["attributes"]["JOINTS_0"]).astype(np.int64))
            w = g.acc(pr["attributes"]["WEIGHTS_0"]).astype(np.float64)
            Wt.append(w)
        self.J = np.vstack(J)
        w = np.vstack(Wt)
        s = w.sum(1, keepdims=True)
        self.W = np.where(s > 0, w / np.maximum(s, 1e-9), 0)
        self.count = len(self.J)


# --- the game's own stance animations ------------------------------------------

def _quat(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


class Clips:
    """Animation clips baked onto a body skeleton by cgf-converter (a glTF with the
    body skin and the game's .dba stance databases)."""

    def __init__(self, path):
        self.g = Gltf(path)
        j = self.g.j
        self.nodes = j["nodes"]
        self.parent = {c: i for i, nd in enumerate(self.nodes) for c in nd.get("children", [])}
        self.by_name = {a.get("name"): a for a in j.get("animations", [])}

    def names(self):
        return list(self.by_name)

    def _sample(self, sampler, t):
        times = self.g.acc(sampler["input"])[:, 0]
        vals = self.g.acc(sampler["output"]).astype(np.float64)
        if len(times) == 1 or t <= times[0]:
            return vals[0]
        if t >= times[-1]:
            return vals[-1]
        k = int(np.searchsorted(times, t)) - 1
        a = (t - times[k]) / max(times[k + 1] - times[k], 1e-9)
        v = vals[k] * (1 - a) + vals[k + 1] * a
        return v / np.linalg.norm(v) if len(v) == 4 else v

    def _trs(self, clip, t):
        anim = self.by_name[clip]
        trs = {}
        for ch in anim["channels"]:
            tgt = ch["target"]
            trs.setdefault(tgt["node"], {})[tgt["path"]] = self._sample(anim["samplers"][ch["sampler"]], t)
        return trs

    def _subtree(self, name):
        start = next((i for i, nd in enumerate(self.nodes) if nd.get("name") == name), None)
        out, todo = set(), [start] if start is not None else []
        while todo:
            i = todo.pop()
            out.add(i)
            todo += self.nodes[i].get("children", [])
        return out

    def pose(self, clip, t=0.0, lower=None):
        """{node name: posed world 4x4} at time t (seconds) of a clip. lower: another clip
        for everything below the spine (for upper-body-only clips)."""
        trs = self._trs(clip, t)
        if lower:
            upper = self._subtree("Spine")
            base = self._trs(lower, t)
            trs = {i: (trs.get(i, {}) if i in upper else base.get(i, {})) for i in set(trs) | set(base)}
        world = {}

        def W(i):
            if i in world:
                return world[i]
            nd = self.nodes[i]
            L = _local(nd)
            if i in trs:
                T = np.eye(4)
                tr = trs[i]
                T[:3, 3] = tr.get("translation", L[:3, 3])
                R = _quat(tr["rotation"]) if "rotation" in tr else L[:3, :3] / np.linalg.norm(L[:3, :3], axis=0)
                S = np.diag(tr.get("scale", np.linalg.norm(L[:3, :3], axis=0)))
                T[:3, :3] = R @ S
                L = T
            p = self.parent.get(i)
            world[i] = (W(p) if p is not None and p != i else np.eye(4)) @ L
            return world[i]
        return {nd.get("name") or f"node{i}": W(i) for i, nd in enumerate(self.nodes)}


def clip_deltas(rig: "Rig", posed: dict) -> dict:
    """Per-joint deltas (posed world @ rest world^-1) for the rig's joints."""
    return {n: posed[n] @ np.linalg.inv(rig.rest[n]) for n in rig.names if n in posed}


# --- maths -----------------------------------------------------------------------

def rot(axis, deg):
    axis = np.asarray(axis, float)
    axis = axis / np.linalg.norm(axis)
    a = math.radians(deg)
    x, y, z = axis
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def align(a, b):
    """Smallest rotation taking direction a to direction b."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if c < -0.999999:
        perp = np.cross(a, [1, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1, 0])
        return rot(perp, 180)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


def about(point, R):
    """4x4 rotation R about a world point."""
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = point - R @ point
    return M


def apply(M, p):
    return M[:3, :3] @ p + M[:3, 3]


def frame(forward, up):
    """Rotation whose columns are (right, up, back) for a forward/up pair (glTF: forward = -Z)."""
    f = forward / np.linalg.norm(forward)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    return np.column_stack([r, u, -f])


# --- posing ----------------------------------------------------------------------

class Poser:
    """World-space deltas for the master skeleton, built joint by joint in hierarchy order."""

    def __init__(self, rig: Rig):
        self.rig = rig
        self.order = self._topo()
        self.rules = {}

    def _topo(self):
        seen, order = set(), []

        def visit(n):
            if n in seen or n is None:
                return
            seen.add(n)
            visit(self.rig.parent.get(n))
            order.append(n)
        for n in self.rig.names:
            visit(n)
        return order

    def pos(self, name):
        return self.rig.rest[name][:3, 3]

    def solve(self, root_delta=np.eye(4), base=None):
        """Deltas per joint. base: a starting pose (deltas, e.g. from a game clip); joints
        without rules keep their place in it relative to their parent."""
        D = {}
        for n in self.order:
            p = self.rig.parent.get(n)
            Dp = D[p] if p in D else root_delta
            if n in self.rules:
                D[n] = self.rules[n](Dp)
            elif base is not None and n in base:
                Bp = base.get(p, np.eye(4)) if p is not None else np.eye(4)
                D[n] = Dp @ np.linalg.inv(Bp) @ base[n]
            else:
                D[n] = Dp
        return D

    def pos_in(self, D, name):
        """A joint's position under deltas D."""
        return apply(D[name], self.pos(name))

    def turn(self, joint, R):
        """Rotate a joint's subtree by world rotation R about the joint (after its parent's delta)."""
        def rule(Dp, joint=joint, R=R):
            return about(apply(Dp, self.pos(joint)), R) @ Dp
        prev = self.rules.get(joint)
        self.rules[joint] = (lambda Dp, prev=prev, rule=rule: rule(prev(Dp))) if prev else rule

    def ik(self, a, b, c, target, pole, end_dir=None):
        """Two-bone IK: put joint c at target, bending at b toward the pole direction.
        end_dir optionally turns c so its first child direction points that way."""
        target, pole = np.asarray(target, float), np.asarray(pole, float)
        l1 = np.linalg.norm(self.pos(b) - self.pos(a))
        l2 = np.linalg.norm(self.pos(c) - self.pos(b))
        state = {}

        def rule_a(Dp):
            pa = apply(Dp, self.pos(a))
            to_t = target - pa
            d = np.clip(np.linalg.norm(to_t), abs(l1 - l2) + 1e-4, l1 + l2 - 1e-4)
            u = to_t / np.linalg.norm(to_t)
            # elbow/knee position in the plane of (shoulder, target, pole)
            pv = pole - np.dot(pole, u) * u
            pv = pv / np.linalg.norm(pv) if np.linalg.norm(pv) > 1e-6 else np.array([0, -1.0, 0])
            x = (l1 * l1 - l2 * l2 + d * d) / (2 * d)
            h = math.sqrt(max(l1 * l1 - x * x, 0))
            pb = pa + u * x + pv * h
            cur_b = apply(Dp, self.pos(b))
            Da = about(pa, align(cur_b - pa, pb - pa)) @ Dp
            state["pb"], state["pc"] = pb, pa + u * d
            return Da

        def rule_b(Dp):
            pb = state["pb"]
            cur_c = apply(Dp, self.pos(c))
            return about(pb, align(cur_c - pb, state["pc"] - pb)) @ Dp

        self.rules[a] = rule_a
        self.rules[b] = rule_b
        if end_dir is not None:
            kids = [n for n, p in self.rig.parent.items() if p == c]
            if kids:
                kid = max(kids, key=lambda k: np.linalg.norm(self.pos(k) - self.pos(c)))

                def rule_c(Dp, kid=kid):
                    pc = apply(Dp, self.pos(c))
                    cur = apply(Dp, self.pos(kid)) - pc
                    return about(pc, align(cur, np.asarray(end_dir, float))) @ Dp
                self.rules[c] = rule_c

    def orient(self, joint, R_target):
        """Give a joint (and its subtree) the world rotation R_target, about the joint."""
        R_rest = self.rig.rest[joint][:3, :3]
        prev = self.rules.get(joint)

        def rule(Dp, joint=joint):
            if prev is not None:
                Dp = prev(Dp)
            pc = apply(Dp, self.pos(joint))
            return about(pc, R_target @ (Dp[:3, :3] @ R_rest).T) @ Dp
        self.rules[joint] = rule

    def curl(self, side, angles):
        """Close the fingers toward the palm (degrees per segment, knuckle first).
        Hand frame: x along the fingers, palm on -z, so +angle about y curls them."""
        R_hand = self.rig.rest[f"{side}Hand"][:3, :3]
        for finger, degs in angles.items():
            for k, deg in enumerate(degs, 1):
                j = f"{side}Hand{finger}{k}"
                if j not in self.rig.rest:
                    continue

                def rule(Dp, j=j, deg=deg):
                    axis = Dp[:3, :3] @ R_hand[:, 1]
                    return about(apply(Dp, self.pos(j)), rot(axis, deg)) @ Dp
                self.rules[j] = rule



def skin(verts, J, W, names, D, fallback=np.eye(4)):
    """Linear blend skinning with per-joint deltas (missing joints use their nearest posed ancestor)."""
    mats = np.stack([D.get(n, fallback) for n in names])
    out = np.zeros_like(verts)
    vh = np.c_[verts, np.ones(len(verts))]
    for c in range(J.shape[1]):
        M = mats[J[:, c]]
        out += W[:, c:c + 1] * np.einsum("nij,nj->ni", M, vh)[:, :3]
    return out


def expand(D, rig: Rig, master_parent):
    """Deltas for a piece's own joints: helper joints take their nearest ancestor's delta."""
    out = {}
    for n in rig.names:
        k = n
        while k is not None and k not in D:
            k = rig.parent.get(k) or master_parent.get(k)
        out[n] = D[k] if k in D else np.eye(4)
    return out


# --- poses -----------------------------------------------------------------------

def _helpers(scene):
    """World positions of all named non-mesh nodes in a trimesh scene."""
    out = {}
    geo = set(scene.graph.nodes_geometry)
    for n in scene.graph.nodes:
        if n in geo:
            continue
        try:
            out[n] = scene.graph.get(n)[0][:3, 3]
        except Exception:
            continue
    return out


class Weapon:
    """A primary weapon mesh with the points that matter for posing, in its own frame:
    the right-hand grip, the left-hand (support) wrist target, muzzle, sight and stock.

    mag: optional path to the magazine exported on its own. StarBreaker leaves a
    weapon's loaded magazine in the wrong frame (often a fan of reload-animation
    copies); the stand-alone magazine placed on the magAttach helper fits the well."""

    def __init__(self, path, female=False, mag=None, kind="stocked"):
        import trimesh
        self.kind = kind if kind in KINDS else "stocked"
        sc = trimesh.load(str(path), force="scene")
        h = _helpers(sc)
        skip = _subtree(sc, "magAttach") if mag is not None and "magAttach" in h else set()
        parts = []
        for n in sc.graph.nodes_geometry:
            if n in skip:
                continue
            T, g = sc.graph[n]
            m = sc.geometry[g].copy()
            m.apply_transform(T)
            parts.append(m)
        if skip:
            mm = trimesh.load(str(mag), force="scene")
            mm = mm.to_geometry() if hasattr(mm, "to_geometry") else mm.dump(concatenate=True)
            # the attach helper is in CryEngine's Z-up frame; the stand-alone export is Y-up
            turn = trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0])
            mm.apply_transform(sc.graph.get("magAttach")[0] @ turn)
            parts.append(mm)
        self.mesh = _drop_crumbs(trimesh.util.concatenate(parts))
        lo, hi = self.mesh.bounds
        self.center = (lo + hi) / 2
        trig = h.get("trigger")
        if trig is None:
            trig = next((h[k] for k in sorted(h) if k.lower().startswith("trigger")), None)
        if trig is None:
            trig = self.center.copy()
        # Every gun is authored in the same frame, the one the game mounts on the hand's
        # weapon bone: barrel along -Z, top along +Y (all muzzle helpers sit at -Z).
        # Guessing it from the mesh failed on guns without muzzle or trigger helpers.
        ax, sign = 2, -1.0
        key = "L_IkGripTarget_HumanFemale" if female and "L_IkGripTarget_HumanFemale" in h else "L_IkGripTarget"
        self.forward = np.array([0, 0, -1.0])
        self.up = np.array([0, 1.0, 0])
        self.right = np.cross(self.forward, self.up)
        # Right hand: the weapon's origin is where it attaches to the hand (the pistol
        # grip; the game mounts it there). Fall back to a point below and behind the trigger.
        o = np.zeros(3)
        no_trigger = not any(k.lower().startswith("trigger") for k in h)
        if no_trigger or (np.linalg.norm(o - trig) < 0.15 and np.dot(o - trig, self.up) < 0):
            self.rgrip = o
        else:
            self.rgrip = trig - self.up * 0.04 - self.forward * 0.05
        # Left hand: the game's IK target is the support hand's wrist (for pistols it is
        # beside the grip: the support hand wraps round the shooting hand).
        fore = h.get(key)
        if fore is not None and (self.kind == "pistol" or np.dot(fore - self.rgrip, self.forward) > 0.0):
            self.lwrist = fore.copy()
        else:
            fore = next((h[k] for k in ("grip", "frontHolder", "underbarrel_attachment") if k in h
                         and np.dot(h[k] - trig, self.forward) > 0.08), None)
            if fore is None:
                fore = trig + self.forward * 0.25
            fore = fore - np.dot(fore - trig, self.right) * self.right
            self.lwrist = fore - self.up * 0.05 - self.right * 0.05
        # the support palm faces the barrel line above the wrist
        bore = self.lwrist - np.dot(self.lwrist - trig, self.right) * self.right + self.up * 0.06
        self.lpalm = (bore - self.lwrist) / np.linalg.norm(bore - self.lwrist)
        # back of the stock: the far end of the gun opposite the muzzle, at the stock's height
        stock = h.get("stock", trig)
        self.butt = stock.copy()
        self.butt[ax] = lo[ax] if sign > 0 else hi[ax]
        # where the eye lines up when aiming (the game's ADS point), else just above the grip
        ads = h.get("ADS_align")
        self.ads = ads.copy() if ads is not None else self.rgrip + self.up * 0.17
        # shouldered weapons: the point that rests on the shoulder
        pad = h.get("shoulderpad")
        self.pad = pad.copy() if pad is not None else self._underside(self.rgrip - self.forward * 0.18)

    def _underside(self, at):
        """The bottom of the body above the grip line at `at` (where a tube without a
        shoulder-pad helper sits on the shoulder)."""
        try:
            sec = self.mesh.section(plane_origin=at, plane_normal=self.forward)
        except Exception:
            sec = None
        top = np.empty(0)
        if sec is not None:
            # points every 5 mm along the cross-section's outline
            pts = [np.linspace(a, b, max(2, int(np.linalg.norm(b - a) / 0.005) + 1))
                   for line in sec.discrete for a, b in zip(line[:-1], line[1:])]
            pts = np.vstack(pts) if pts else sec.vertices
            d = pts - at
            u, r = d @ self.up, d @ self.right
            top = u[(np.abs(r) < 0.05) & (u > 0.03)]
        return at + self.up * (float(top.min()) if len(top) else 0.06)

    def adopt(self, base: "Weapon") -> None:
        """Hold this gun exactly like its base model: a skin or store variant takes the
        base gun's grip, support-hand, sight, shoulder and direction (and its kind),
        keeping only its own mesh."""
        for k in ("kind", "forward", "up", "right", "rgrip", "lwrist", "lpalm", "ads", "pad", "butt"):
            setattr(self, k, np.array(getattr(base, k), copy=True) if k != "kind" else base.kind)

    def placement(self, forward, up, anchor_local, anchor_world):
        """4x4 putting the weapon's anchor point at anchor_world, barrel along forward."""
        R = frame(forward, up) @ frame(self.forward, self.up).T
        M = np.eye(4)
        M[:3, :3] = R
        M[:3, 3] = np.asarray(anchor_world) - R @ anchor_local
        return M


def _drop_crumbs(mesh, cell=0.006):
    """Remove small loose bits that float away from the gun (they would print as
    separate crumbs). Connected surfaces are grouped with any others they come within
    about two grid cells of; the main group is kept with any sizeable group next to it."""
    import trimesh
    from scipy import ndimage
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    v, f = mesh.vertices, mesh.faces
    if len(f) == 0:
        return mesh
    comp = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(f))
    pts = np.vstack([v[f].reshape(-1, 3), v[f].mean(1)])
    owner = np.concatenate([np.repeat(comp, 3), comp])
    idx = np.floor((pts - pts.min(0)) / cell).astype(int)
    grid = np.zeros(idx.max(0) + 1, bool)
    grid[tuple(idx.T)] = True
    lab, n = ndimage.label(ndimage.binary_dilation(grid), structure=np.ones((3, 3, 3)))
    nc = int(comp.max()) + 1
    g = lab[tuple(idx.T)] - 1 + nc
    A = coo_matrix((np.ones(len(g)), (owner, g)), shape=(nc + n, nc + n))
    _, group = connected_components(A, directed=False)
    area = np.bincount(group[comp], weights=mesh.area_faces, minlength=group.max() + 1)
    # Keep every real part (some guns are built from pieces that don't quite touch):
    # groups with a fair share of the surface that sit next to the main body. Tiny
    # groups, and pieces left floating away from the gun (parts the export put in a
    # reload position), are dropped.
    main = int(area.argmax())
    fg = group[comp]
    cen = v[f].mean(1)
    mlo, mhi = cen[fg == main].min(0), cen[fg == main].max(0)
    ok = np.zeros(len(area), bool)
    for gi in np.nonzero(area >= 0.03 * area.max())[0]:
        glo, ghi = cen[fg == gi].min(0), cen[fg == gi].max(0)
        gap = np.maximum(0, np.maximum(glo - mhi, mlo - ghi)).max()
        ok[gi] = gi == main or gap < 0.05
    keep = ok[fg]
    if keep.all():
        return mesh
    m = mesh.copy()
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    return m

def _subtree(scene, root):
    """Names of root and every node below it in a trimesh scene graph."""
    kids = {}
    for parent, child, *_ in scene.graph.to_edgelist():
        kids.setdefault(parent, []).append(child)
    out, todo = set(), [root]
    while todo:
        n = todo.pop()
        if n in out:
            continue
        out.add(n)
        todo += kids.get(n, [])
    return out


def _hand_frame(x, z):
    """Hand rotation from finger direction x and back-of-hand direction z (palm is -z)."""
    x = np.asarray(x, float) / np.linalg.norm(x)
    z = np.asarray(z, float)
    z = z - np.dot(z, x) * x
    z /= np.linalg.norm(z)
    return np.column_stack([x, np.cross(z, x), z])


# finger curl per segment (degrees, knuckle first)
GRIP = {"Index": (30, 35, 20), "Middle": (65, 70, 35), "Ring": (65, 70, 35), "Pinky": (65, 70, 35)}
SUPPORT = {"Index": (35, 40, 25), "Middle": (45, 50, 30), "Ring": (50, 55, 30), "Pinky": (55, 55, 30)}
LOOSE = {"Index": (8, 12, 8), "Middle": (12, 18, 10), "Ring": (15, 20, 10), "Pinky": (18, 22, 12)}


# The game mounts a held weapon (its origin, the grip) on RightWeaponBone, a child of
# RightHand. Its local transform from the skeleton (bhm_skeleton_v7 / bhf_skeleton_v2),
# CryEngine Z-up, turned into glTF axes (x, z, -y).
def _weapon_bone_local():
    Q = np.eye(4)
    Q[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    L = np.eye(4)
    L[:3, :3] = _quat([2.98e-8, -2.24e-8, -0.7071059, 0.7071076])   # x, y, z, w
    L[:3, 3] = [0.09, 0.03, 0.0]
    return Q @ L @ Q.T


WEAPON_BONE = _weapon_bone_local()

# The game's stance clips per weapon kind: (clip, clip for the legs or None)
STANCES = {
    ("stocked", "stand"): ("stocked_alerted_stand_idle_01", None),
    ("stocked", "kneel"): ("stocked_alerted_crouch_idle_01", None),
    # pistols standing: the game's standing combat body, arms out to the pistol by IK
    # (the standing pistol clip is a high hold with the hands at the face)
    ("pistol", "stand"): ("stocked_alerted_stand_idle_01", None),
    ("pistol", "kneel"): ("pistol_alerted_crouch_idle_01", None),
    ("shouldered", "stand"): ("shouldered_alerted_stand_idle_01", None),
    ("shouldered", "kneel"): ("shouldered_alerted_crouch_idle_01", None),
}
RELAXED_CLIP = "nw_neutral_stand_idle_to_nw_alerted_stand_idle"   # its first frame: standing idle, no weapon


def _clip_pose(rig: Rig, clips: Clips, pose: str, weapon: Weapon):
    """(deltas, weapon placement) from the game's own stance for the weapon's kind, or
    None when the install has no such clip. The weapon sits on RightWeaponBone as in
    the game and the support hand goes to the weapon's own grip target. The low ready
    keeps the game's stance and tips the weapon down, the arms following it."""
    stance = "kneel" if pose.endswith("_kneel") else "stand"
    spec = STANCES.get((weapon.kind, stance))
    if spec is None or spec[0] not in clips.by_name or (spec[1] and spec[1] not in clips.by_name):
        return None
    posed = clips.pose(spec[0], 0.0, lower=spec[1])
    if not {"RightHand", "Hips", "Spine", "RightArm", "LeftArm", "Neck"} <= set(posed) or "RightHand" not in rig.rest:
        return None

    def turn(names, pivot, R):
        A = about(pivot, R)
        for n in names:
            posed[n] = A @ posed[n]

    # Level the aim (the upper body turns about the spine until the barrel is level),
    # then face forward: some stances turn the whole body (the pistol's bladed crouch).
    f = (posed["RightHand"] @ WEAPON_BONE)[:3, :3] @ weapon.forward
    flat = np.array([f[0], 0.0, f[2]])
    if np.linalg.norm(flat) > 1e-6:
        upper = [clips.nodes[i].get("name") for i in clips._subtree("Spine")]
        turn([n for n in upper if n in posed], posed["Spine"][:3, 3], align(f, flat))
        hips = posed["Hips"][:3, 3]
        turn(list(posed), np.array([hips[0], 0.0, hips[2]]), align(flat, np.array([0, 0, -1.0])))
    base = clip_deltas(rig, posed)
    p = Poser(rig)
    level_f, level_u = np.array([0, 0, -1.0]), np.array([0, 1.0, 0])
    if weapon.kind == "pistol" and stance == "stand":
        # both arms out, sights at eye height in front of the face
        neck, sh = posed["Neck"][:3, 3], posed["RightArm"][:3, 3]
        eye_y = neck[1] + 0.115
        grip = np.array([neck[0] + 0.02, min(eye_y - 0.015 - float(np.dot(weapon.ads - weapon.rgrip, weapon.up)),
                                             sh[1] + 0.10), 0.0])                  # head drops to the sights
        reach = 0.96 * _arm_reach(p, "Right")                     # arms nearly straight
        dx, dy = grip[0] - sh[0], grip[1] - sh[1]
        grip[2] = sh[2] - math.sqrt(max(reach * reach - dx * dx - dy * dy, 0.09))
        M_w = weapon.placement(level_f, level_u, weapon.rgrip, grip)
        if pose.startswith("ready"):
            M_w = about((posed["RightArm"][:3, 3] + posed["LeftArm"][:3, 3]) / 2, rot([1, 0, 0], -40)) @ M_w
        R = M_w[:3, :3]
        w_fwd, w_up, w_right = R @ weapon.forward, R @ weapon.up, R @ weapon.right
        Rr = _hand_frame(w_fwd - w_up * 0.55, w_right)
        p.ik("RightArm", "RightForeArm", "RightHand", apply(M_w, weapon.rgrip) - Rr @ np.array([0.07, 0.0, -0.03]),
             pole=[0.8, -1.0, 0.3])
        p.ik("LeftArm", "LeftForeArm", "LeftHand", apply(M_w, weapon.lwrist), pole=[-0.8, -1.0, 0.3])
        p.orient("RightHand", Rr)
        p.orient("LeftHand", _hand_frame(w_fwd - w_up * 0.9, -w_right))
        p.curl("Right", GRIP)
        p.curl("Left", GRIP)
        return p.solve(base=base), M_w
    M_w = posed["RightHand"] @ WEAPON_BONE
    if pose.startswith("ready") and weapon.kind == "stocked":
        # the game's lowered stance: across the body, stock at the right chest, muzzle
        # down toward the left side; both hands follow the gun
        M_w = _lowered(weapon, posed["RightArm"][:3, 3], posed["LeftArm"][:3, 3])
        R = M_w[:3, :3]
        Rr = _hand_frame(R @ weapon.forward - R @ weapon.up * 0.55, R @ weapon.right)
        p.ik("RightArm", "RightForeArm", "RightHand", apply(M_w, weapon.rgrip) - Rr @ np.array([0.07, 0.0, -0.03]),
             pole=[1.0, -0.6, 0.6])
        p.orient("RightHand", Rr)
        p.ik("LeftArm", "LeftForeArm", "LeftHand", apply(M_w, weapon.lwrist), pole=[-0.6, -1.0, 0.4])
        p.orient("LeftHand", _hand_frame(R @ weapon.forward, -(R @ weapon.lpalm)))
        p.curl("Right", GRIP)
        p.curl("Left", SUPPORT)
        return p.solve(base=base), M_w
    if pose.startswith("ready"):
        pivot = apply(M_w, weapon.pad if weapon.kind == "shouldered" else weapon.butt)
        if weapon.kind == "pistol":
            pivot = (posed["RightArm"][:3, 3] + posed["LeftArm"][:3, 3]) / 2
        A = about(pivot, rot([0, 1, 0], 10) @ rot([1, 0, 0], -22 if weapon.kind == "shouldered" else -38))
        M_w = A @ M_w
        hand = A @ posed["RightHand"]
        p.ik("RightArm", "RightForeArm", "RightHand", hand[:3, 3], pole=[1.0, -1.0, 0.3])
        p.orient("RightHand", hand[:3, :3])
    p.ik("LeftArm", "LeftForeArm", "LeftHand", apply(M_w, weapon.lwrist),
         pole=[-0.8, -1.0, 0.3] if weapon.kind == "pistol" else [-0.6, -1.0, 0.0])
    return p.solve(base=base), M_w


def _lowered(w: Weapon, rsh, lsh) -> np.ndarray:
    """Placement for the lowered (low ready) rifle stance, from the shoulder positions:
    the gun lies across the front of the body, nearly parallel to it, the stock at the
    right chest and the muzzle pointing down to the left; its top faces outward."""
    rsh, lsh = np.asarray(rsh, float), np.asarray(lsh, float)
    fwd = np.array([-0.86, -0.40, -0.30])            # left, down, a little forward
    fwd /= np.linalg.norm(fwd)
    out = np.array([0.0, 0.25, -1.0])                # sights face away from the chest
    up = out - fwd * float(np.dot(out, fwd))
    up /= np.linalg.norm(up)
    width = float(np.linalg.norm(rsh - lsh))
    butt = rsh + np.array([-0.06 * width / 0.35, -0.13, -0.16])   # right chest, in front
    return w.placement(fwd, up, w.butt, butt)


def _arm_reach(p: Poser, side: str) -> float:
    return float(np.linalg.norm(p.pos(f"{side}ForeArm") - p.pos(f"{side}Arm"))
                 + np.linalg.norm(p.pos(f"{side}Hand") - p.pos(f"{side}ForeArm")))


def _hold(p: Poser, weapon: Weapon, pose: str, y_shift: float) -> np.ndarray:
    """Weapon placement (4x4) for its kind and the pose: aiming, or the low ready."""
    aim = pose.startswith("aim")
    eye = p.pos("Neck") + np.array([0.035, 0.115, -0.095]) + [0, y_shift, 0]   # right eye
    sh = p.pos("RightArm") + [0, y_shift, 0]                                     # right shoulder
    level_f, level_u = np.array([0, 0, -1.0]), np.array([0, 1.0, 0])
    w = weapon
    if w.kind == "pistol":
        if aim:
            # two hands, arms out, sights at eye height
            grip = [0.02, eye[1] - 0.015 - float(np.dot(w.ads - w.rgrip, w.up)), sh[2] - 0.50]
            return w.placement(level_f, level_u, w.rgrip, grip)
        f = np.array([0, -0.72, -0.69])                  # muzzle down and forward, in front of the belly
        return w.placement(f, np.cross(np.cross(f, level_u), f), w.rgrip, [0.04, sh[1] - 0.36, sh[2] - 0.30])
    if w.kind == "shouldered":
        # the tube rests on top of the right shoulder; low ready tips the muzzle down about it
        top = sh + [-0.03, 0.065, 0.0]
        M = w.placement(level_f, level_u, w.pad, top)
        # wide tubes: move out along the shoulder until the gun clears the head
        head = p.pos("Neck") + np.array([0.0, 0.13, -0.02]) + [0, y_shift, 0]
        for _ in range(12):
            v = w.mesh.vertices @ M[:3, :3].T + M[:3, 3]
            if not (np.linalg.norm(v - head, axis=1) < 0.12).any():
                break
            M[0, 3] += 0.01
            top = top + [0.01, 0, 0]
        return M if aim else about(top, rot([1, 0, 0], -22)) @ M
    if w.kind == "heavy":
        # carried at the right hip, the left hand on the gun's top handle
        hip = p.pos("RightUpLeg") + [0, y_shift, 0]
        grip = hip + np.array([0.15, 0.07, -0.05])
        M = w.placement(level_f, level_u, w.rgrip, grip)
        lsh = p.pos("LeftArm") + [0, y_shift, 0]
        reach = 0.95 * _arm_reach(p, "Left")
        for _ in range(40):                               # slide it back until the left hand reaches
            if np.linalg.norm(apply(M, w.lwrist) - lsh) <= reach:
                break
            M[2, 3] += 0.015
        return M if aim else about(apply(M, w.rgrip), rot([1, 0, 0], -18)) @ M
    # stocked: butt in the shoulder pocket, sights at eye height
    M = w.placement(level_f, level_u, w.butt,
                    [0.11, eye[1] - 0.015 - float(np.dot(w.ads - w.butt, w.up)), sh[2] - 0.06])
    if aim:
        return M
    return _lowered(w, sh, p.pos("LeftArm") + [0, y_shift, 0])


def build_pose(rig: Rig, pose: str, weapon: Weapon | None, back_z: float, clips: "Clips | None" = None):
    """(deltas per joint, weapon placement or None) for one of POSES. With the game's
    stance clips (clips), held weapons use the game's own stance for their kind."""
    if (clips is not None and weapon is not None and pose == "ready_stand" and weapon.kind == "stocked"
            and RELAXED_CLIP in clips.by_name):
        # lowered rifle: the game's upright standing idle, the gun across the body
        p = Poser(rig)
        posed = clips.pose(RELAXED_CLIP, 0.0)
        base = clip_deltas(rig, posed)
        M_w = _lowered(weapon, posed["RightArm"][:3, 3], posed["LeftArm"][:3, 3])
        R = M_w[:3, :3]
        Rr = _hand_frame(R @ weapon.forward - R @ weapon.up * 0.55, R @ weapon.right)
        p.ik("RightArm", "RightForeArm", "RightHand", apply(M_w, weapon.rgrip) - Rr @ np.array([0.07, 0.0, -0.03]),
             pole=[1.0, -0.6, 0.6])
        p.orient("RightHand", Rr)
        p.ik("LeftArm", "LeftForeArm", "LeftHand", apply(M_w, weapon.lwrist), pole=[-0.6, -1.0, 0.4])
        p.orient("LeftHand", _hand_frame(R @ weapon.forward, -(R @ weapon.lpalm)))
        p.curl("Right", GRIP)
        p.curl("Left", SUPPORT)
        p.turn("Neck", rot([1, 0, 0], -8))
        return p.solve(base=base), M_w
    if clips is not None and weapon is not None and pose != "relaxed":
        got = _clip_pose(rig, clips, pose, weapon)
        if got is not None:
            return got
    p = Poser(rig)
    kneel = pose.endswith("_kneel")
    drop = KNEEL_DROP if kneel else 0.0
    root = np.eye(4)
    root[1, 3] = -drop
    lift = 0.06 if kneel else 0.0   # kneeling keeps the torso a little more upright
    if kneel:
        # left foot planted forward with the knee up; right knee on the ground, foot behind
        lf, rf = p.pos("LeftFoot"), p.pos("RightFoot")
        # front foot flat as in the rest pose; back foot on its toes, sole facing back
        flat = p.pos("LeftToeBase") - lf if "LeftToeBase" in rig.rest else np.array([0, -0.4, -1.0])
        p.ik("LeftUpLeg", "LeftLeg", "LeftFoot", [lf[0] - 0.02, lf[1], -0.42], pole=[0, 0.3, -1], end_dir=flat)
        p.ik("RightUpLeg", "RightLeg", "RightFoot", [rf[0] + 0.02, 0.20, 0.55], pole=[0, -1.0, 0],
             end_dir=[0, -0.6, 0.8])
        p.turn("Spine1", rot([1, 0, 0], -8))  # lean into the weapon
    relaxed_base = None
    if (pose == "relaxed" or weapon is None) and clips is not None and RELAXED_CLIP in clips.by_name:
        relaxed_base = clip_deltas(rig, clips.pose(RELAXED_CLIP, 0.0))   # the game's standing idle
    if pose == "relaxed" or weapon is None:
        # arms hanging at the sides, elbows slightly back, hands by the thighs
        for side, s in (() if relaxed_base else (("Left", -1.0), ("Right", 1.0))):
            sh = p.pos(f"{side}Arm")
            p.ik(f"{side}Arm", f"{side}ForeArm", f"{side}Hand", [s * 0.25, sh[1] - 0.58, sh[2] - 0.02],
                 pole=[s * 0.2, 0, 1.0], end_dir=[s * 0.05, -1.0, -0.05])
            p.curl(side, LOOSE)
        M_w = None
        if weapon is not None and weapon.kind == "pistol":
            # holstered on the outside of the right thigh, muzzle down, grip back
            thigh = p.pos("RightUpLeg")
            M_w = weapon.placement(np.array([0, -1.0, 0]), np.array([0, 0, -1.0]), weapon.center,
                                   [thigh[0] + 0.12, thigh[1] - 0.17, thigh[2] + 0.01])
        elif weapon is not None:
            # on the back as the game carries it: straight down the spine, stock at the top
            # and barrel toward the floor, lying on its side, then moved in until it rests
            # against the back
            fwd = np.array([0.0, -1.0, 0.0])
            side_up = np.cross([0, 0, 1.0], fwd)
            top = p.pos("Neck")[1] - 0.10                      # stock just below the shoulders
            M_w = weapon.placement(fwd, side_up, weapon.butt, [0.0, top, back_z])
            z = (weapon.mesh.vertices @ M_w[:3, :3].T + M_w[:3, 3])[:, 2]
            M_w[2, 3] += back_z + 0.005 - float(np.percentile(z, 3))
        return (p.solve(base=relaxed_base) if relaxed_base else p.solve(root)), M_w
    M_w = _hold(p, weapon, pose, lift - drop)
    aim = pose.startswith("aim")
    # head: cheek down toward the sights when aiming, eyes on the muzzle at the low ready
    p.turn("Neck", rot([1, 0, 0], -12 if aim else -6))
    if aim and weapon.kind in ("stocked", "shouldered"):
        p.turn("Neck", rot([0, 0, 1], -8))
    if weapon.kind == "pistol":
        pole_r, pole_l = [0.8, -1.0, 0.3], [-0.8, -1.0, 0.3]
    elif weapon.kind == "heavy":
        pole_r, pole_l = [0.6, -0.3, 1.0], [-0.6, -1.0, 0.2]
    else:
        pole_r, pole_l = [1.0, -1.0, 0.2 if aim else 0.3], [-0.5 if aim else -0.6, -1.0, 0.0]
    R = M_w[:3, :3]
    w_fwd, w_up, w_right = R @ weapon.forward, R @ weapon.up, R @ weapon.right
    # Right hand round the pistol grip: fingers forward and a little down, back of the
    # hand outward, thumb up; the grip sits in the palm ahead of the wrist.
    Rr = _hand_frame(w_fwd - w_up * 0.55, w_right)
    rh = apply(M_w, weapon.rgrip) - Rr @ np.array([0.07, 0.0, -0.03])
    if weapon.kind == "pistol":
        # support hand wrapped round the shooting hand from the left, fingers down the grip
        Rl = _hand_frame(w_fwd - w_up * 0.9, -w_right)
    else:
        # under the handguard at the game's support-hand target, palm toward the barrel
        Rl = _hand_frame(w_fwd, -(R @ weapon.lpalm))
    lh = apply(M_w, weapon.lwrist)
    p.ik("RightArm", "RightForeArm", "RightHand", rh, pole=pole_r)
    p.ik("LeftArm", "LeftForeArm", "LeftHand", lh, pole=pole_l)
    p.orient("RightHand", Rr)
    p.orient("LeftHand", Rl)
    p.curl("Right", GRIP)
    p.curl("Left", GRIP if weapon.kind == "pistol" else SUPPORT)
    return p.solve(root), M_w
