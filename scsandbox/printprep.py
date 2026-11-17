"""Make game meshes printable.

Game models are built for rendering, not printing: hundreds of separate open
shells, overlapping panels, zero-thickness decals, LOD and collision copies,
interiors, and details (antennas, gun barrels, fins) that end up one or two
nozzle widths wide once a 20 m ship is scaled to 150 mm. This module runs the
routine a person would run by hand in Blender/Meshmixer and writes a file a
slicer accepts.

The routine (solid mode)
  1. Clean    drop LOD/collision/damage/shield/decal nodes (`exclude_nodes`)
              and parts the user chose to remove.
  2. Fix up   thicken parts the user picked to a set size (radial scale about
              the part's long axis, so a barrel gets fatter, not longer).
  3. Pose     Y-up -> Z-up, optional display tilt, scale to the target size.
  4. Fuse     voxelize every surface at print resolution, seal hairline gaps,
              fill enclosed space and drop floating specks. The result is one
              closed, manifold body no matter how broken the source was.
  5. Thicken  find slender features thinner than the minimum (default three
              nozzle widths) by morphological opening, keep the ones that stick
              out (antennas, barrels, fins; not the edges of the hull) and grow
              them about their medial axis until they reach the minimum.
              Loose pieces near the model (a gun beside its turret, held by a
              gimbal the game leaves out) get a strut to the main body.
  6. Hollow   optional, for resin: keep a wall of `hollow_mm`.
  7. Stand    optional: a display stand, either a separate piece with a peg
              that fits a socket cut into the model, or fused to the model.
  8. Mesh     the surface is the source geometry offset by SKIN_MM (unsigned
              distance, so flipped game normals don't matter) unioned with the
              filled core; marching cubes, light smoothing, crumbs dropped,
              decimated to `max_faces`, placed on the bed.

Parts mode keeps each part as its own body (for kits and painting), repairs
it, thickens slender parts to the minimum and can add a separate stand.

Game units are meters, glTF is Y-up; output is millimeters, Z-up, sitting on
the bed (z = 0) and centered on the origin.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import trimesh

Y_UP_TO_Z_UP = trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0])
# The ship on its stand: nose to +x (the right, seen from the stand's front, the -y
# side with the name plate), right wing toward the front (-y), top up (+z). The export
# has the nose along +y once turned Z-up.
NOSE_RIGHT = trimesh.transformations.rotation_matrix(-math.pi / 2, [0, 0, 1])
PRINT_FORMATS = ("3mf", "stl", "obj", "ply")
# Gaps in the hull up to about twice this (at print scale) are sealed before filling.
SEAL_MM = 0.8
# Blur (in voxels) applied to the occupancy grid before meshing; removes staircase
# artefacts while keeping anything at least ~3 voxels across.
SMOOTH_SIGMA_VOXELS = 1.0
# The printed surface is the source geometry offset outward by this much (also gives
# zero-thickness panels some body).
SKIN_MM = 0.15
# The stand is mostly flat faces: this many triangles keeps its lettering crisp.
STAND_MAX_FACES = 600_000
# Loose pieces (a gun beside its turret) up to this far from the model, or this share of
# the print size if that is more, get a strut to the main body.
JOIN_GAP_MM = 3.0
JOIN_GAP_SHARE = 0.03
# Disconnected pieces smaller than this many triangles are dropped after meshing.
MIN_PIECE_FACES = 400
# Always skipped, on top of exclude_nodes (older settings.json files pin an outdated list):
# texture-only overlay sheets floating just above the hull, which the surface skin would
# otherwise turn into rows of bumps. dec_* = decals, *_pom = parallax-occlusion layers,
# glow/screen = emissive and display planes.
# Not damage parts named *_2D_dmg / *_broken: on ships like the Eclipse those are the
# wing sections the game can shear off, not copies of them.
OVERLAY_NODES = [r"(?i)^dec_", r"(?i)(^|_)poms?(_|$)", r"(?i)^(linked)?glow", r"(?i)^screen_"]
# "helper" nodes are usually empty pivots, but some carry real hull geometry (the
# Eclipse's canopy frame hangs on helper_canopy_rotation_*). Those are kept when their
# material is a real surface material; helper/proxy/no-draw materials still go.
HELPER_PATTERN = re.compile(r"(?i)helper")
NOT_A_SURFACE = re.compile(r"(?i)helper|proxy|nodraw|no_draw|invisible|physics|collision")
# The same overlays as material layers inside ordinary parts (needs material names in
# the export, so the print export keeps them).
OVERLAY_MATERIAL = re.compile(r"(?i)(pom|decal|glow|rtt_|emissive)")
# Glow and emissive parts are usually flat light sheets, but some are solid trim: the
# Alpha Wolf's thruster nozzle rims are "glow_thruster_main" / "Glow_Trim" parts. Those
# are kept unless they are flat: thinner than this share of their length, or than
# GLOW_MIN_M (game meters; overlays sit a few millimeters off the hull).
GLOW = re.compile(r"(?i)glow|emissive")
FLAT_SHARE = 0.03
GLOW_MIN_M = 0.02


def _is_flat(m: trimesh.Trimesh) -> bool:
    """A sheet (decal, light strip) rather than a solid shape."""
    try:
        ext = np.sort(m.bounding_box_oriented.primitive.extents)
    except Exception:
        ext = np.sort(m.extents)
    return ext[-1] <= 0 or ext[0] <= max(FLAT_SHARE * ext[-1], GLOW_MIN_M)


@dataclass
class PrintOptions:
    size_mm: float = 150.0
    scale: float = 0.0              # N of a 1:N scale; when set it wins over size_mm
    mode: str = "solid"
    voxel_mm: float = 0.2
    max_voxels: int = 250_000_000
    max_faces: int = 4_000_000
    format: str = "3mf"
    exclude_nodes: list[str] = field(default_factory=list)
    smooth: bool = True
    # strength: features thinner than nozzle_mm * min_feature_nozzles get thickened
    nozzle_mm: float = 0.4
    min_feature_nozzles: float = 3.0
    thicken: bool = True
    # per-part choices from the "find thin parts" list: {part name: {"action": "remove"|"thicken"|"auto", "mm": 1.5}}
    part_overrides: dict = field(default_factory=dict)
    hollow_mm: float = 0.0          # 0 = solid; resin prints usually use 1.5-2.5
    tilt_deg: float = 0.0           # incline: nose up (+) or down (-)
    roll_deg: float = 0.0           # tilt: right wing down (+, toward the stand's front) or left (-)
    stand: str = "none"             # none | peg (separate stand, socket in model) | attached
    stand_height_mm: float = 30.0   # gap between the stand's base and the model
    stand_shape: str = "round"      # round | rect (rectangular base with a text plate)
    stand_text: str = ""            # raised text on the rect base's front strip
    stand_text2: str = ""           # optional second, smaller line under it
    stand_scale: bool = False       # add the print scale (1:85) to the plate's second line
    stand_logo: str = ""            # PNG mask of a logo embossed beside the text
    stand_posts: int = 1            # 1, or 2 posts (fore and aft) for long or heavy prints
    stand_base_w_mm: float = 0.0    # base width along the ship (round: its diameter); 0 = auto
    stand_base_d_mm: float = 0.0    # rectangular base depth (front to back); 0 = auto
    stand_base_color: str = ""      # painted 3MF: base and post filament (#rrggbb); "" = filament 1
    stand_logo_color: str = ""      # painted 3MF: logo filament
    stand_text_color: str = ""      # painted 3MF: first plate line filament
    stand_text2_color: str = ""     # painted 3MF: second plate line filament
    stand_forward_mm: float = 0.0   # move the stand peg toward the nose (+) or tail (-)
    stand_side_mm: float = 0.0      # move the stand peg to the right (+) or left (-)
    paint_colors: int = 0           # 3MF only: paint the model in this many filament colors (Bambu Studio)
    figure: bool = False            # character figure: the stand goes under its feet

    @classmethod
    def from_settings(cls, s: dict, **override) -> "PrintOptions":
        p = dict(s.get("print", {}))
        p.update({k: v for k, v in override.items() if v is not None})
        return cls(**{k: p[k] for k in cls.__dataclass_fields__ if k in p})

    @property
    def min_feature_mm(self) -> float:
        return max(0.0, self.nozzle_mm * self.min_feature_nozzles)


# --- loading, per-part fixes, pose --------------------------------------------

def load_parts(glb: Path, exclude: list[str]) -> list[tuple[str, trimesh.Trimesh]]:
    scene = trimesh.load(str(glb), force="scene")
    soft = [re.compile(p) for p in exclude if HELPER_PATTERN.search(p)]
    patterns = [re.compile(p) for p in [*exclude, *OVERLAY_NODES] if not HELPER_PATTERN.search(p)
                and not GLOW.search(p)]
    glow_nodes = [re.compile(p) for p in [*exclude, *OVERLAY_NODES] if GLOW.search(p)]
    parts = []
    for node in scene.graph.nodes_geometry:
        try:
            transform, geom_name = scene.graph[node]
        except ValueError:  # node not reachable from the scene root
            continue
        if any(p.search(node) or p.search(geom_name) for p in patterns):
            continue
        geom = scene.geometry.get(geom_name)
        if not isinstance(geom, trimesh.Trimesh) or len(geom.faces) == 0:
            continue
        mat = getattr(getattr(geom.visual, "material", None), "name", None) or ""
        if any(p.search(node) or p.search(geom_name) for p in soft) and (not mat or NOT_A_SURFACE.search(mat)):
            continue
        glowing = any(p.search(node) or p.search(geom_name) for p in glow_nodes) or bool(mat and GLOW.search(mat))
        if mat and OVERLAY_MATERIAL.search(mat) and not GLOW.search(mat):
            continue  # decal / parallax layer inside an otherwise normal part
        m = geom.copy()
        # glTF primitives often share one vertex buffer; trimesh keeps the whole buffer
        # on each part, and those foreign vertices would land all over the model.
        m.remove_unreferenced_vertices()
        if glowing and _is_flat(m):
            continue  # a light sheet; solid glowing trim (nozzle rims) stays
        m.apply_transform(transform)
        m.metadata["rgb"] = _part_color(geom, mat)  # for painted 3MF exports
        m.metadata["material"] = mat
        m.visual = trimesh.visual.ColorVisuals(m)
        parts.append((node, m))
    if not parts:
        raise ValueError(f"No printable geometry in {glb.name} (everything was empty or excluded)")
    return parts + _fill_glassless_canopies(scene, dict(parts))


NEUTRAL_RGB = (138, 140, 142)   # textured parts whose color lives in the texture
# surfaces nobody sees on an intact ship: the hull's inside, damage and interior layers
HIDDEN_SURFACE = re.compile(r"(?i)internal|internals|interior|(^|[_\s])dmg|damage|_int_|/int_|cockpit_int")


def _part_color(geom, mat_name: str) -> tuple[int, int, int]:
    """A part's paint color in sRGB 0-255: its material's base color (stored linear by
    glTF). Plain white is a textured material whose color is in its texture: neutral grey."""
    mat = getattr(geom.visual, "material", None)
    f = getattr(mat, "baseColorFactor", None)
    if f is None:
        f = getattr(mat, "diffuse", None)
    if f is None:
        return NEUTRAL_RGB
    lin = np.asarray(f[:3], float) / 255.0
    if np.all(lin > 0.99) and "white" not in mat_name.lower():
        return NEUTRAL_RGB
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return tuple(int(round(v * 255)) for v in np.clip(srgb, 0, 1))


def paint_faces(mesh: trimesh.Trimesh, parts, colors: int, log=print) -> tuple[np.ndarray, list[str]]:
    """Paint the printed surface from the original model: each output triangle takes the
    color of the nearest original surface, and the colors are reduced to `colors`
    filaments (k-means over the colors, weighted by the area they cover). Returns
    (filament number 1..colors per face, filament colors as #RRGGBB, most used first)."""
    from scipy.spatial import cKDTree
    pts, nrm, cols = [], [], []
    # the hull's inside faces and damage layers sit right behind the skin: leave them out
    seen = [(n, m) for n, m in parts if not HIDDEN_SURFACE.search(f"{n} {m.metadata.get('material', '')}")] or parts
    # Sample every surface evenly by area, not at its triangles' centers: a big panel made
    # of a few large triangles had almost no samples, so the dense small parts beside and
    # behind it won (the Stingray's red wings came out in ragged red and grey blotches).
    rng = np.random.default_rng(7)
    total_area = sum(float(m.area) for _, m in seen) or 1.0
    budget = 4_000_000
    for _, m in seen:
        area = np.asarray(m.area_faces, float)
        if not len(area) or area.sum() <= 0:
            continue
        k = int(max(8, round(budget * area.sum() / total_area)))
        fi = rng.choice(len(area), size=k, p=area / area.sum())
        tri = np.asarray(m.triangles)[fi]
        u, v = rng.random(k), rng.random(k)
        flip = u + v > 1
        u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
        c = tri[:, 0] + u[:, None] * (tri[:, 1] - tri[:, 0]) + v[:, None] * (tri[:, 2] - tri[:, 0])
        pts.append(c)
        nrm.append(np.asarray(m.face_normals)[fi])
        cols.append(np.tile(np.array(m.metadata.get("rgb", NEUTRAL_RGB), float), (k, 1)))
    pts, nrm, cols = np.vstack(pts), np.vstack(nrm), np.vstack(cols)
    # Of the nearest original surfaces, take the closest one facing the same way: on a
    # thin wing the panel on the far side is about as near, and would speckle the paint.
    centers, normals = np.asarray(mesh.triangles_center), np.asarray(mesh.face_normals)
    dist, idx = cKDTree(pts).query(centers, k=8, workers=-1)
    facing = np.abs(np.einsum("ij,ikj->ik", normals, nrm[idx]))   # game panels face either way
    score = dist * (1.6 - facing)                                  # nearer and better aligned wins
    # Hull layers often lie on top of each other (the Stingray's red paint panels over its
    # light metal ones); of the candidates about as near as the best, take the outermost,
    # the one you see. Picking by distance alone flipped between them triangle by triangle.
    best = np.min(score, axis=1, keepdims=True)
    close = (score <= best * 1.5 + 0.05) & (facing > 0.6)
    close[np.arange(len(idx)), np.argmin(score, axis=1)] = True
    outward = np.einsum("ikj,ij->ik", pts[idx] - centers[:, None, :], normals)
    near = idx[np.arange(len(idx)), np.argmax(np.where(close, outward, -np.inf), axis=1)]
    face_rgb = cols[near]
    area = np.asarray(mesh.area_faces)
    uniq, inv = np.unique(face_rgb, axis=0, return_inverse=True)
    inv = inv.ravel()
    weight = np.bincount(inv, weights=area, minlength=len(uniq))
    k = int(max(1, min(colors, len(uniq))))
    # k-means++ seeding then Lloyd, on the few distinct colors (area-weighted)
    rng = np.random.default_rng(1)
    centers = [uniq[np.argmax(weight)]]
    for _ in range(1, k):
        d = np.min([np.sum((uniq - c) ** 2, axis=1) for c in centers], axis=0) * weight
        centers.append(uniq[rng.choice(len(uniq), p=d / d.sum())] if d.sum() > 0 else uniq[0])
    centers = np.array(centers, float)
    for _ in range(50):
        lab = np.argmin(((uniq[:, None, :] - centers[None]) ** 2).sum(-1), axis=1)
        new = np.array([np.average(uniq[lab == j], axis=0, weights=weight[lab == j]) if np.any(lab == j) else centers[j]
                        for j in range(k)])
        if np.allclose(new, centers):
            break
        centers = new
    lab = np.argmin(((uniq[:, None, :] - centers[None]) ** 2).sum(-1), axis=1)
    # Colors nobody could tell apart share a filament: the Stingray's black matte metal and
    # its dark secondary panels lie on top of each other, and as two filaments the wings
    # came out blotched in both.
    centers, lab, k = _merge_close_colors(centers, lab, weight, k)
    order = np.argsort(-np.bincount(lab, weights=weight, minlength=k))   # most area first
    rank = np.empty(k, int)
    rank[order] = np.arange(k)
    faces = rank[lab[inv]] + 1
    faces = _smooth_labels(mesh, faces, k)
    hexes = ["#%02X%02X%02X" % tuple(int(round(v)) for v in centers[j]) for j in order]
    log(f"  painted in {k} color(s): {', '.join(hexes)}")
    return faces.astype(np.int32), hexes


# Filament colors closer than this (CIE76 delta E) are merged into one
SAME_COLOR_DE = 12.0


def _lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB 0-255 to CIE L*a*b* (D65)."""
    c = np.asarray(rgb, float) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _merge_close_colors(centers: np.ndarray, lab: np.ndarray, weight: np.ndarray, k: int):
    """Merge the two nearest filament colors while they're within SAME_COLOR_DE; the
    merged color is their area-weighted mix. Returns (centers, labels, k)."""
    centers = np.array(centers, float)
    lab = lab.copy()
    while k > 1:
        L = _lab(centers)
        d = np.sqrt(((L[:, None] - L[None]) ** 2).sum(-1))
        np.fill_diagonal(d, np.inf)
        i, j = np.unravel_index(np.argmin(d), d.shape)
        if d[i, j] >= SAME_COLOR_DE:
            break
        i, j = min(i, j), max(i, j)
        wi, wj = weight[lab == i].sum(), weight[lab == j].sum()
        centers[i] = (centers[i] * wi + centers[j] * wj) / max(wi + wj, 1e-9)
        lab[lab == j] = i
        lab[lab > j] -= 1
        centers = np.delete(centers, j, axis=0)
        k -= 1
    return centers, lab, k


def _smooth_labels(mesh: trimesh.Trimesh, labels: np.ndarray, k: int, passes: int = 5) -> np.ndarray:
    """Clear specks: a triangle takes the color most of its neighbours share when its own
    is shared by at most one of them (stray triangles and thin strands shrink away)."""
    adj = np.asarray(mesh.face_adjacency)
    if len(adj) == 0:
        return labels
    lab = labels.copy()
    n = len(lab)
    for _ in range(passes):
        votes = np.zeros((n, k + 1), np.int32)
        np.add.at(votes, (adj[:, 0], lab[adj[:, 1]]), 1)
        np.add.at(votes, (adj[:, 1], lab[adj[:, 0]]), 1)
        own = votes[np.arange(n), lab]
        best = np.argmax(votes, axis=1)
        # a triangle sharing its color with at most one neighbour, while two or more share
        # another, takes theirs (stray specks and one-triangle strands both go)
        flip = (own <= 1) & (votes.max(axis=1) >= 2) & (best != lab)
        if not flip.any():
            break
        lab[flip] = best[flip]
    return lab


# Bambu Lab PLA Basic colors (approximate), to say which spool suits each painted slot
BAMBU_PLA_BASIC = {
    "Jade White": "#FFFFFF", "Light Gray": "#D1D3D5", "Silver": "#A6A9AA", "Gray": "#8E9089",
    "Dark Gray": "#545454", "Black": "#000000", "Blue Gray": "#5B6579", "Beige": "#F7E6DE",
    "Gold": "#E4BD68", "Bronze": "#847D48", "Brown": "#9D432C", "Cocoa Brown": "#6F5034",
    "Red": "#C12E1F", "Maroon Red": "#9D2235", "Magenta": "#EC008C", "Pink": "#F55A74",
    "Orange": "#FF6A13", "Pumpkin Orange": "#FF9016", "Yellow": "#F4EE2A",
    "Sunflower Yellow": "#FEC600", "Bright Green": "#BECF00", "Bambu Green": "#00AE42",
    "Mistletoe Green": "#3F8E43", "Turquoise": "#00B1B7", "Cyan": "#0086D6",
    "Cobalt Blue": "#0056B8", "Blue": "#0A2989", "Purple": "#5E43B7", "Indigo Purple": "#482960",
}


def closest_filament(hex_color: str) -> str:
    """The Bambu PLA Basic color nearest to hex_color (weighted RGB distance)."""
    def rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = rgb(hex_color)

    def dist(h):
        r2, g2, b2 = rgb(h)
        rm = (r + r2) / 2
        return (2 + rm / 256) * (r - r2) ** 2 + 4 * (g - g2) ** 2 + (2 + (255 - rm) / 256) * (b - b2) ** 2
    return min(BAMBU_PLA_BASIC, key=lambda n: dist(BAMBU_PLA_BASIC[n]))


def write_bambu_3mf(out: Path, bodies: list[tuple[str, trimesh.Trimesh]], faces: dict[str, np.ndarray],
                    colors: list[str], title: str = "model") -> Path:
    """A 3MF that Bambu Studio opens in its colors: each triangle carries its color as a
    standard 3MF color group entry, which Bambu Studio offers to match to filaments when
    it opens the file. (Bambu's own paint_color marks only filament slots, and Bambu
    drops the filament colors of a 3MF it didn't write itself, so a Stingray came out in
    whatever filaments the printer had loaded.) Bodies without painting stay uncolored."""
    import io
    import zipfile
    from xml.sax.saxutils import escape
    out = Path(out)
    objects, items = [], []
    group = len(bodies) + 1   # the color group's resource id, after the objects
    for k, (name, m) in enumerate(bodies, start=1):
        v = np.asarray(m.vertices, float)
        f = np.asarray(m.faces, int)
        paint = faces.get(name)
        buf = io.StringIO()
        # the 3MF spec wants an object-level property when its triangles carry their own
        props = f' pid="{group}" pindex="0"' if paint is not None and colors else ""
        buf.write(f'  <object id="{k}" type="model" name="{escape(name)}"{props}>\n   <mesh>\n    <vertices>\n')
        buf.writelines(f'     <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>\n' for x, y, z in v)
        buf.write('    </vertices>\n    <triangles>\n')
        for i, (a, b, c) in enumerate(f):
            n = int(paint[i]) if paint is not None else 0
            extra = f' pid="{group}" p1="{n - 1}"' if 0 < n <= len(colors) else ""
            buf.write(f'     <triangle v1="{a}" v2="{b}" v3="{c}"{extra}/>\n')
        buf.write('    </triangles>\n   </mesh>\n  </object>\n')
        objects.append(buf.getvalue())
        items.append(f'  <item objectid="{k}" printable="1"/>\n')
    palette = (f'  <m:colorgroup id="{group}">\n' + "".join(f'   <m:color color="{c.upper()}FF"/>\n' for c in colors)
               + '  </m:colorgroup>\n') if colors else ""
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
             ' xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">\n'
             f' <metadata name="Title">{escape(title)}</metadata>\n'
             ' <metadata name="Application">Meshyard</metadata>\n'
             ' <resources>\n' + palette + "".join(objects) + ' </resources>\n <build>\n' + "".join(items) + ' </build>\n</model>\n')
    # Only the model: with Meshyard's own settings files beside it, Bambu Studio skipped
    # the colors and opened the ship all grey.
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                     ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                     ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>\n')
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return out


def _fill_glassless_canopies(scene, by_node: dict) -> list[tuple[str, trimesh.Trimesh]]:
    """Some exports leave a canopy's glass out (F7C-M Mk II pilot canopy), which prints
    as an open frame showing the cockpit. Glass prints opaque anyway, so close each
    canopy that has no glass part with the convex hull of its frame."""
    g = scene.graph
    children: dict[str, list] = {}
    parent: dict[str, str] = {}
    for a, b, *_ in g.to_edgelist():
        children.setdefault(a, []).append(b)
        parent[b] = a
    is_canopy = lambda n: "canopy" in n.lower() and not re.search(r"(?i)btn|button|switch|hinge|glow|^dec_", n)

    def canopy_above(n):
        while n in parent:
            n = parent[n]
            if is_canopy(n):
                return True
        return False

    out = []
    # an assembly = the topmost canopy node (often a pivot) and everything under it;
    # trimesh splits each mesh into one node per material, so test the whole subtree
    for node in [n for n in g.nodes if is_canopy(n) and not canopy_above(n)]:
        stack, names = [node], []
        while stack:
            n = stack.pop()
            names.append(n)
            stack.extend(children.get(n, []))
        if any("glass" in n.lower() or "glass" in _material(scene, n) for n in names):
            continue
        frame = [by_node[n] for n in names if n in by_node]
        if not frame or sum(len(m.vertices) for m in frame) < 4:
            continue
        pts = np.vstack([m.vertices for m in frame])
        try:
            hull = trimesh.convex.convex_hull(pts)
        except Exception:
            continue
        out.append((f"{node}_glass", hull))
    return out


def _material(scene, node: str) -> str:
    """Lower-case material name of a node's mesh ('' if none)."""
    try:
        geom = scene.geometry.get(scene.graph[node][1])
    except (ValueError, KeyError, TypeError):
        return ""
    return (getattr(getattr(getattr(geom, "visual", None), "material", None), "name", None) or "").lower()


def real_size(parts) -> list[float]:
    """The model's own size in game meters, Z-up: length/width along X and Y, height on Z."""
    lo, hi = _bounds(parts)
    ex = hi - lo
    return [float(ex[0]), float(ex[2]), float(ex[1])]  # Y-up input


def _orient_and_scale(parts, size_mm: float, tilt_deg: float = 0.0, ratio: float = 0.0,
                      roll_deg: float = 0.0, nose_right: bool = True) -> float:
    """Turn the model Z-up with its nose to +x (see NOSE_RIGHT), tilt it (roll_deg about
    its length, right wing down for positive) and incline it (tilt_deg, nose up for
    positive), then size it. ratio is the N of a 1:N scale; without one the longest side
    becomes size_mm. Characters (nose_right False) keep facing the stand's front.
    Returns the scale in mm per game meter."""
    turn = NOSE_RIGHT @ Y_UP_TO_Z_UP if nose_right else Y_UP_TO_Z_UP
    for _, m in parts:
        m.apply_transform(turn)
    lo, hi = _bounds(parts)
    c = (lo + hi) / 2
    rot = np.eye(4)
    if roll_deg:   # about the nose axis: +x
        rot = trimesh.transformations.rotation_matrix(math.radians(roll_deg), [1, 0, 0], c) @ rot
    if tilt_deg:   # about the stand's left-right axis: nose (+x) up for positive
        rot = trimesh.transformations.rotation_matrix(math.radians(-tilt_deg), [0, 1, 0], c) @ rot
    if roll_deg or tilt_deg:
        for _, m in parts:
            m.apply_transform(rot)
        lo, hi = _bounds(parts)
    extent = float(np.max(hi - lo))
    if extent <= 0:
        raise ValueError("Model has zero size")
    scale = 1000.0 / ratio if ratio > 0 else size_mm / extent
    for _, m in parts:
        m.apply_scale(scale)
    return scale


def _bounds(parts):
    return (np.min([m.bounds[0] for _, m in parts], axis=0),
            np.max([m.bounds[1] for _, m in parts], axis=0))


def _obb(mesh: trimesh.Trimesh):
    """(transform, extents) of the oriented bounding box, extents sorted with axis indices."""
    try:
        obb = mesh.bounding_box_oriented
        return obb.primitive.transform, np.asarray(obb.primitive.extents, float)
    except Exception:  # degenerate (flat or tiny) geometry
        lo, hi = mesh.bounds
        return trimesh.transformations.translation_matrix((lo + hi) / 2), hi - lo


def thicken_part(mesh: trimesh.Trimesh, target_mm: float) -> float:
    """Scale a slender part across its long axis so its cross-section is at least target_mm.
    Returns the factor used (1.0 = unchanged)."""
    T, ext = _obb(mesh)
    order = np.argsort(ext)
    thick = ext[order[1]]  # the larger of the two minor dimensions
    if thick <= 1e-9 or thick >= target_mm:
        return 1.0
    k = min(target_mm / thick, 50.0)
    s = np.ones(3)
    s[order[0]] = min(k, target_mm / max(ext[order[0]], 1e-9))
    s[order[1]] = k
    M = T @ np.diag([*s, 1.0]) @ np.linalg.inv(T)
    mesh.apply_transform(M)
    return float(k)


def slender_parts(parts, min_mm: float, limit: int = 60) -> list[dict]:
    """Parts that are rod- or blade-like and thinner than min_mm at print scale
    (antennas, barrels, pitot tubes, fins). Used for the per-part list in the app."""
    out = []
    for name, m in parts:
        _, ext = _obb(m)
        a, b, c = sorted(ext)
        rod = b < max(min_mm * 2, 0.5) and c > 4 * max(b, 1e-6)
        if rod and b < min_mm * 1.5:
            out.append({"name": name, "length_mm": round(float(c), 1), "thickness_mm": round(float(b), 2),
                        "too_thin": bool(b < min_mm)})
    out.sort(key=lambda d: (not d["too_thin"], -d["length_mm"]))
    return out[:limit]


def analyze(glb: Path, opts: PrintOptions) -> dict:
    """What the print would look like before running it: size, scale and the slender parts."""
    parts = load_parts(Path(glb), opts.exclude_nodes)
    real = real_size(parts)
    scale = _orient_and_scale(parts, opts.size_mm, opts.tilt_deg, opts.scale, opts.roll_deg,
                              not opts.figure)
    lo, hi = _bounds(parts)
    return {"parts": len(parts), "scale": _scale_text(scale), "size_mm": [round(float(x), 1) for x in hi - lo],
            "real_m": [round(x, 2) for x in real],
            "min_feature_mm": round(opts.min_feature_mm, 2), "slender": slender_parts(parts, opts.min_feature_mm)}


def apply_overrides(parts, opts: PrintOptions, log=print):
    """Remove or thicken the parts the user picked; auto-thicken slender parts in parts mode."""
    kept = []
    for name, m in parts:
        o = opts.part_overrides.get(name) or {}
        action = o.get("action", "auto")
        if action == "remove":
            log(f"  removed {name}")
            continue
        if action == "thicken" and float(o.get("mm") or 0) > 0:
            k = thicken_part(m, float(o["mm"]))
            if k > 1:
                log(f"  thickened {name} x{k:.1f} to {float(o['mm']):g} mm")
        kept.append((name, m))
    if not kept:
        raise ValueError("Every part was removed")
    return kept


_parts_cache: dict = {}


def _cached_parts(glb: Path, exclude: list[str]):
    """load_parts, kept for the last couple of models: moving the peg asks again and
    again about the same model. Returns copies (posing and scaling change them)."""
    key = (str(glb), glb.stat().st_mtime_ns, tuple(exclude or ()))
    if key not in _parts_cache:
        if len(_parts_cache) >= 2:
            _parts_cache.pop(next(iter(_parts_cache)))
        _parts_cache[key] = load_parts(glb, exclude)
    return [(n, m.copy()) for n, m in _parts_cache[key]]


# the peg preview works on a coarser grid than the print: same choice of spot, seconds not minutes
PEG_PREVIEW_VOXEL_MM = 1.0
PEG_PREVIEW_MAX_VOXELS = 8_000_000


def peg_preview(glb: Path, opts: PrintOptions, log=print) -> dict:
    """Where the stand's peg will go, in the model's own coordinates (the viewer's), for
    the current print settings: the same pick as the print (plan_stand), on a coarser grid.
    Returns {"top": [x, y, z] where the post meets the model, "peg_r", "post_r", "depth"
    (meters), "stand"}, or {"stand": "none"}."""
    if opts.stand == "none":
        return {"stand": "none"}
    parts = _cached_parts(Path(glb), opts.exclude_nodes)
    # a marker inside the model's box, carried through the same turn and scaling, gives
    # the print-to-model transform without re-deriving it
    lo, hi = _bounds(parts)
    c = (lo + hi) / 2
    probe = np.array([c, c + [0.01, 0, 0], c + [0, 0.01, 0], c + [0, 0, 0.01]])
    marker = trimesh.Trimesh(vertices=probe.copy(), faces=[[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], process=False)
    parts.append(("__peg_probe__", marker))
    _orient_and_scale(parts, opts.size_mm, opts.tilt_deg, opts.scale, opts.roll_deg,
                              not opts.figure)
    moved = np.asarray(parts.pop()[1].vertices, float)
    if opts.scale > 0:
        plo, phi = _bounds(parts)
        opts = replace(opts, size_mm=float(np.max(phi - plo)))
    parts = apply_overrides(parts, opts, log)
    coarse = replace(opts, voxel_mm=max(opts.voxel_mm, PEG_PREVIEW_VOXEL_MM),
                     max_voxels=min(opts.max_voxels, PEG_PREVIEW_MAX_VOXELS))
    g = voxelize(parts, coarse, extra_mm=0.0, log=lambda *_: None)
    spec = plan_stand(g, opts, log=lambda *_: None)
    # model = A @ (print - t): solve the linear part from the probe's three edges
    A_print = (moved[1:] - moved[0]).T          # print-space edges
    A_model = (probe[1:] - probe[0]).T          # model-space edges
    to_model = A_model @ np.linalg.inv(A_print)
    def model(p):
        return probe[0] + to_model @ (np.asarray(p, float) - moved[0])

    def r4(v):
        return [round(float(x), 4) for x in v]
    top = model([spec.x, spec.y, spec.top_z])
    mm = float(np.linalg.norm(to_model @ np.array([1.0, 0, 0])))  # meters per print mm
    # the whole stand as the export builds it (base, post up to the model), in the same
    # model coordinates: the post runs straight down in print space, so on a tilted
    # model it leans in the model's own frame
    post_bottom_z = spec.top_z - opts.stand_height_mm
    base_bottom_z = post_bottom_z - spec.base_t
    if spec.shape == "rect" or opts.stand_shape == "rect":
        x0, x1, y0, y1 = _base_box(spec)
        base = {"shape": "rect", "corners": [r4(model([x, y, z])) for z in (base_bottom_z, post_bottom_z)
                                             for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]}
    else:
        x0, x1, y0, y1 = _base_box(spec)
        bx, by = (x0 + x1) / 2, (y0 + y1) / 2
        base = {"shape": "round", "bottom": r4(model([bx, by, base_bottom_z])),
                "top": r4(model([bx, by, post_bottom_z])), "r": round(spec.base_r * mm, 4)}
    # the stand's own axes in model coordinates, so the viewer can show it square on:
    # x toward the nose (the stand's right), y toward the stand's back, z up
    axes = {k: r4(to_model @ e / np.linalg.norm(to_model @ e)) for k, e in
            (("x", np.array([1.0, 0, 0])), ("y", np.array([0, 1.0, 0])), ("z", np.array([0, 0, 1.0])))}
    posts = all_posts(spec)
    result = {"stand": opts.stand, "top": r4(top), "axes": axes,
              "tops": [r4(model([px, py, ptz])) for px, py, ptz, _ in posts],
              "post_bottoms": [r4(model([px, py, post_bottom_z])) for px, py, _, _ in posts],
              "incline": opts.tilt_deg, "tilt": opts.roll_deg,
              "balance": None if spec.balance is None else round(spec.balance, 1),
              "peg_r": round(spec.peg_r * mm, 4), "post_r": round(spec.post_r * mm, 4),
              "depth": round(spec.peg_depth * mm, 4),
              "down": r4(to_model @ np.array([0, 0, -1.0]) / mm),
              "post_bottom": r4(model([spec.x, spec.y, post_bottom_z])), "base": base}
    # the stand as the export builds it (plate, lettering, logo), coarser, for the viewer
    try:
        result["stand_glb"] = str(_stand_preview_glb(Path(glb), spec, opts, base_bottom_z, model))
    except Exception as e:  # noqa: BLE001  the simple stand still shows
        log(f"stand preview skipped: {e}")
    return result


STAND_PREVIEW_VERSION = 1
STAND_GREY, PLATE_GREY = "#5D636B", "#C9CED6"


def _stand_preview_glb(glb: Path, spec: StandSpec, opts: PrintOptions, base_bottom_z: float, model) -> Path:
    """The stand mesh (as exported, at preview resolution) in the model's coordinates,
    colored by part: the base, logo and text in their picked colors. Cached by settings."""
    import hashlib
    key = json.dumps([STAND_PREVIEW_VERSION, glb.name, glb.stat().st_mtime_ns, repr(spec), opts.stand,
                      opts.stand_height_mm, opts.stand_base_color, opts.stand_logo_color,
                      opts.stand_text_color, opts.stand_text2_color], default=str)
    out = glb.parent / "stand_previews" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".glb")
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    height = opts.stand_height_mm
    m = make_stand_mesh(spec, height, 0.3, peg=opts.stand == "peg", smooth=opts.smooth, preview=True)
    lab = np.maximum(stand_face_labels(m.vertices, m.faces, spec, 0.0, height), 0)
    v = np.asarray(m.vertices, float).copy()
    v[:, 2] += base_bottom_z
    mv = np.array([model(p) for p in v])
    base = opts.stand_base_color or STAND_GREY
    colors = [base, opts.stand_logo_color or (base if opts.stand_base_color else PLATE_GREY),
              opts.stand_text_color or (base if opts.stand_base_color else PLATE_GREY),
              opts.stand_text2_color or (base if opts.stand_base_color else PLATE_GREY)]
    scene = trimesh.Scene()
    for k in range(4):
        sel = np.nonzero(lab == k)[0]
        if not len(sel):
            continue
        part = trimesh.Trimesh(mv, np.asarray(m.faces)[sel], process=False)
        part.remove_unreferenced_vertices()
        h = colors[k].lstrip("#")
        rgba = [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [255]
        part.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(
            name=f"stand_{STAND_PARTS[k]}", baseColorFactor=rgba, metallicFactor=0.0, roughnessFactor=0.6))
        scene.add_geometry(part, node_name=f"stand_{STAND_PARTS[k]}")
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(scene.export(file_type="glb"))
    tmp.replace(out)
    return out


def measure(glb: Path, exclude: list[str]) -> dict:
    """The model's real size in game meters (the parts a print would keep)."""
    real = real_size(load_parts(Path(glb), exclude))
    return {"real_m": [round(x, 3) for x in real], "longest_m": round(max(real), 3)}


def file_tag(opts: PrintOptions) -> str:
    """The size part of an export's file name: 1-500 for a scale, 150mm for a size."""
    if opts.scale > 0:
        return "1-" + f"{round(opts.scale, 1):g}".replace(".", "_")
    return f"{int(opts.size_mm)}mm"


def _scale_text(scale: float) -> str:
    """scale is mm per game meter."""
    ratio = 1000 / scale
    return f"1:{ratio:.0f}" if ratio >= 1 else f"{1 / ratio:.2f}:1"


# --- voxel routine ------------------------------------------------------------

def _surface_points(mesh: trimesh.Trimesh, pitch: float) -> np.ndarray:
    """Points on the surface dense enough that every voxel it crosses gets one."""
    v, f = trimesh.remesh.subdivide_to_size(mesh.vertices, mesh.faces, max_edge=pitch * 0.9, max_iter=16)
    return np.vstack([v[np.unique(f)], v[f].mean(axis=1)])  # only vertices the faces use


class Grid:
    """Boolean occupancy grid; voxel (i, j, k) covers origin + [i, i+1) * pitch."""

    def __init__(self, lo, hi, pitch: float, pad: int):
        self.pitch = pitch
        self.origin = np.asarray(lo, float) - pad * pitch
        self.shape = tuple((np.ceil((np.asarray(hi) - lo) / pitch) + 1 + 2 * pad).astype(int))
        self.v = np.zeros(self.shape, dtype=bool)
        # exact shapes merged into the surface when meshing, as f(X, Y, z) -> signed
        # distance in mm (negative inside): added (stand) and cut (peg socket)
        self.add_sdf: list = []
        self.cut_sdf: list = []

    def index(self, pts) -> np.ndarray:
        idx = np.floor((np.asarray(pts) - self.origin) / self.pitch).astype(np.int64)
        return np.clip(idx, 0, np.array(self.shape) - 1)

    def centers(self, axis: int) -> np.ndarray:
        return self.origin[axis] + (np.arange(self.shape[axis]) + 0.5) * self.pitch

    def to_mesh(self, smooth: bool, parts=None, log=print) -> trimesh.Trimesh:
        """Mesh the occupancy. With the source parts, the surface follows the original
        geometry (signed distance near the boundary) instead of voxel faces."""
        from scipy import ndimage
        from skimage.measure import marching_cubes
        field, level = None, 0.5
        if smooth and parts:
            field = self._signed_field(parts, log)
            level = 0.0
        if field is None:
            field = np.pad(self.v, 2).astype(np.float32)
            if smooth:
                # A hard 0/1 grid meshes as voxel-sized terraces; blurring it first gives
                # a field whose 0.5 level crosses between voxel centers.
                ndimage.gaussian_filter(field, sigma=SMOOTH_SIGMA_VOXELS, output=field, truncate=3.0)
        elif self.add_sdf or self.cut_sdf:
            self._apply_exact(field)
        verts, faces, _, _ = marching_cubes(field, level=level)
        del field
        verts = (verts - 2 + 0.5) * self.pitch + self.origin
        # marching_cubes already shares vertices; merging by distance would pinch thin sheets
        mesh = trimesh.Trimesh(verts, faces, process=False)
        mesh = _drop_small_pieces(mesh, MIN_PIECE_FACES)
        if smooth:
            trimesh.smoothing.filter_taubin(mesh, iterations=2)
        mesh.fix_normals()
        return mesh

    def _apply_exact(self, field) -> None:
        """Merge the exact shapes into a distance field (padded by 2 voxels), slice by
        slice: the stand's flat faces and bevel come out flat rather than terraced, and
        the socket is cut after the skin, which would otherwise seal its mouth again."""
        p = self.pitch
        xs = self.origin[0] + (np.arange(field.shape[0]) - 2 + 0.5) * p
        ys = self.origin[1] + (np.arange(field.shape[1]) - 2 + 0.5) * p
        X, Y = np.meshgrid(xs, ys, indexing="ij")
        for k in range(field.shape[2]):
            z = float(self.origin[2] + (k - 2 + 0.5) * p)
            sl = field[:, :, k]
            for f in self.add_sdf:
                np.minimum(sl, f(X, Y, z), out=sl)
            for f in self.cut_sdf:
                np.maximum(sl, -f(X, Y, z), out=sl)

    def _signed_field(self, parts, log=print):
        """Field (mm, negative inside) padded by 2 voxels whose zero level is the union of
        the filled core and a thin skin around the original surface.

        Game meshes have flipped and double-sided panels, so normals can't be trusted to
        say which side is outside. Instead: skin = everything within SKIN_MM of the source
        triangles (unsigned distance, no normals needed); core = the voxel solid pulled one
        voxel inward so it stays under the skin. Their union's outer surface is the source
        geometry offset by SKIN_MM, and the core only shows where it really sticks out
        (sealed gaps, thickened features). Returns None if no surface is near the boundary."""
        from scipy import ndimage
        from scipy.spatial import cKDTree
        p, band_w = self.pitch, 2
        cap = np.float32((band_w + 1) * p)
        # thinner than ~0.9 voxel and the sampled sheet breaks up between voxel centers
        skin = np.float32(max(SKIN_MM, 0.9 * p))
        cross = ndimage.generate_binary_structure(3, 1)
        edge = self.v & ~ndimage.binary_erosion(self.v, structure=cross)  # inner boundary layer
        near = ndimage.binary_dilation(edge, structure=cross, iterations=1)
        band = ndimage.binary_dilation(near, structure=cross, iterations=band_w - 1)
        del edge
        samples = []
        for _, m in parts:
            v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=p * 0.5, max_iter=24)
            pts = np.vstack([v[np.unique(f)], v[f].mean(axis=1)])
            idx = self.index(pts)
            keep = near[idx[:, 0], idx[:, 1], idx[:, 2]]
            if keep.any():
                samples.append(pts[keep].astype(np.float32))
        del near
        if not samples or sum(len(s) for s in samples) < 100:
            return None
        pts = np.concatenate(samples)
        del samples
        log(f"  fitting the surface to the original geometry ({len(pts):,} samples)")
        tree = cKDTree(pts)
        bi = np.nonzero(band)
        del band
        centers = self.origin + (np.stack(bi, axis=1) + 0.5) * p
        d, _ = tree.query(centers, workers=-1, distance_upper_bound=float(cap))
        del tree, pts, centers
        d = np.minimum(d, cap).astype(np.float32)
        # core: blurred occupancy as an approximate distance, shifted one voxel inward
        field = self.v.astype(np.float32)
        ndimage.gaussian_filter(field, sigma=SMOOTH_SIGMA_VOXELS, output=field, truncate=3.0)
        # occupancy 1 -> -p (well inside), surface at occupancy 0.75: about half a voxel in
        field = (0.5 - field) * np.float32(4 * p) + np.float32(p)
        field[bi] = np.minimum(field[bi], d - skin)
        return np.pad(field, 2, constant_values=cap)


def _drop_small_pieces(mesh: trimesh.Trimesh, min_faces: int) -> trimesh.Trimesh:
    """Remove disconnected crumbs (stray bits of skin or geometry) from a meshed solid."""
    labels = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(mesh.faces))
    counts = np.bincount(labels)
    keep = counts[labels] >= min_faces
    if keep.all():
        return mesh
    mesh.update_faces(keep)
    mesh.remove_unreferenced_vertices()
    return mesh


def _ball(r: int) -> np.ndarray:
    r = max(int(r), 1)
    z, y, x = np.ogrid[-r:r + 1, -r:r + 1, -r:r + 1]
    return (x * x + y * y + z * z) <= r * r + 0.5


def voxelize(parts, opts: PrintOptions, extra_mm: float, log=print) -> Grid:
    from scipy import ndimage
    lo, hi = _bounds(parts)
    pitch = opts.voxel_mm
    need = np.prod(np.ceil((hi - lo + 2 * extra_mm) / pitch) + 1)
    if need > opts.max_voxels:
        pitch *= (need / opts.max_voxels) ** (1 / 3)
        log(f"  model is large: using {pitch:.3f} mm voxels to stay within memory")
    pad = 2 + int(math.ceil(extra_mm / pitch))
    seal = max(1, int(round(SEAL_MM / pitch)))
    pad = max(pad, seal + 2)
    g = Grid(lo, hi, pitch, pad)
    for _, m in parts:
        idx = g.index(_surface_points(m, pitch))
        g.v[idx[:, 0], idx[:, 1], idx[:, 2]] = True
    surface = g.v
    # Game hulls are open shells full of seams, intakes and gaps, so "fill what is
    # enclosed" alone leaves a one-voxel skin. Close gaps up to ~2 * SEAL_MM first,
    # then treat a voxel as inside if it is enclosed, or has hull on all six sides.
    cross = ndimage.generate_binary_structure(3, 1)
    closed = ndimage.binary_dilation(surface, structure=cross, iterations=seal)
    closed = ndimage.binary_erosion(closed, structure=cross, iterations=seal, border_value=0)
    closed |= surface
    g.v = ndimage.binary_fill_holes(closed) | _enclosed_six_ways(closed)
    g.v = ndimage.binary_fill_holes(g.v)
    _drop_specks(g.v, 27)
    return g


def _enclosed_six_ways(v: np.ndarray) -> np.ndarray:
    """Voxels with occupied voxels somewhere along all six axis directions."""
    inside = v.copy()
    inside[...] = True
    for ax in range(3):
        inside &= np.logical_or.accumulate(v, axis=ax)
        inside &= np.flip(np.logical_or.accumulate(np.flip(v, ax), axis=ax), ax)
    return inside


def _drop_specks(v: np.ndarray, min_voxels: int) -> None:
    from scipy import ndimage
    labels, n = ndimage.label(v)
    if n > 1:
        sizes = ndimage.sum(v, labels, range(1, n + 1))
        v &= np.isin(labels, np.nonzero(sizes >= min_voxels)[0] + 1)


# loose pieces under this share of the main body, still apart after joining, are debris
LOOSE_SHARE = 0.005


def drop_loose_bits(g: Grid, log=print) -> int:
    """Remove small pieces left floating free of the model after connect_pieces: specks
    of an emblem or a gear part the game hangs in mid-air. In a print they come off as
    loose bits or fall over on the bed. Returns pieces removed."""
    from scipy import ndimage
    labels, n = ndimage.label(g.v)
    if n <= 1:
        return 0
    sizes = ndimage.sum(g.v, labels, range(1, n + 1))
    keep = np.nonzero(sizes >= LOOSE_SHARE * sizes.max())[0] + 1
    dropped = n - len(keep)
    if dropped:
        g.v &= np.isin(labels, keep)
        log(f"  removed {dropped} loose bit(s) too small to print on their own")
    return dropped


def drop_small_shells(mesh: trimesh.Trimesh, log=print) -> trimesh.Trimesh:
    """Remove tiny shells the surface fit leaves behind where a thin bit pinched off
    (a few cubic mm next to a gun or fin). Returns the mesh without them."""
    shells = mesh.split(only_watertight=False)
    if len(shells) <= 1:
        return mesh
    vols = np.array([abs(s.volume) for s in shells])
    keep = [s for s, v in zip(shells, vols) if v >= LOOSE_SHARE * vols.max()]
    if len(keep) == len(shells):
        return mesh
    log(f"  removed {len(shells) - len(keep)} tiny loose shell(s)")
    return trimesh.util.concatenate(keep)


def connect_pieces(g: Grid, min_mm: float, max_gap_mm: float, log=print) -> int:
    """Join loose pieces to the main body with a short strut at their closest point.

    Guns sit on their turret or remote turret through a gimbal with a small gap or a
    pivot the game doesn't model, so after fusing a barrel can come out as its own shell:
    it prints as a separate part, or falls off the bed. Each piece within max_gap_mm of
    the main body gets a strut at least min_mm thick (or two voxels) across the gap.
    Returns pieces joined."""
    from scipy import ndimage
    labels, n = ndimage.label(g.v)
    if n <= 1:
        return 0
    sizes = ndimage.sum(g.v, labels, range(1, n + 1))
    main = int(np.argmax(sizes)) + 1
    reach = int(math.ceil(max_gap_mm / g.pitch)) + 1
    r = max(2, int(math.ceil(max(min_mm, 0.6) / 2 / g.pitch)))
    ball = _ball(r)
    joined, far = 0, 0
    for lab, box in enumerate(ndimage.find_objects(labels), start=1):
        if lab == main or box is None:
            continue
        sl = tuple(slice(max(0, s.start - reach), min(dim, s.stop + reach)) for s, dim in zip(box, g.shape))
        sub = labels[sl]
        if not (sub == main).any():
            far += 1
            continue
        dist, idx = ndimage.distance_transform_edt(sub != main, return_indices=True)
        piece = sub == lab
        d = np.where(piece, dist, np.inf)
        a = np.unravel_index(int(np.argmin(d)), d.shape)
        if d[a] * g.pitch > max_gap_mm:
            far += 1
            continue
        b = tuple(int(idx[k][a]) for k in range(3))
        a0 = np.array(a) + [s.start for s in sl]
        b0 = np.array(b) + [s.start for s in sl]
        # a strut from inside the piece to inside the body, so both ends overlap
        length = float(np.linalg.norm(b0 - a0))
        over = r / max(length, 1.0)  # reach one strut radius past each end
        for t in np.linspace(-over, 1 + over, 2 * int(np.ceil(length + 2 * r)) + 3):
            c = np.round(a0 + (b0 - a0) * t).astype(int)
            lo = np.maximum(c - r, 0)
            hi = np.minimum(c + r + 1, g.shape)
            bl = ball[tuple(slice(l - (cc - r), h - (cc - r)) for l, h, cc in zip(lo, hi, c))]
            g.v[tuple(slice(l, h) for l, h in zip(lo, hi))] |= bl
        joined += 1
    if joined:
        log(f"  joined {joined} loose piece(s) (guns on turrets and the like) to the model")
    if far:
        log(f"  {far} piece(s) more than {max_gap_mm:g} mm from the model were left loose")
    return joined


def thicken_thin_features(g: Grid, min_mm: float, log=print) -> dict:
    """Grow slender features (antennas, barrels, fins) to at least min_mm thick.

    Opening with a ball of diameter min_mm removes everything thinner than that.
    Of what's removed, sharp edges and corners of the hull touch the bulk along
    their whole length; features that stick out touch it only at their root. Those
    are kept, reduced to their medial axis (skeleton) and re-grown to min_mm.
    """
    from scipy import ndimage
    from skimage.morphology import skeletonize

    r = min_mm / 2 / g.pitch
    if r < 1.0:
        return {"thickened": 0, "note": f"detail ({g.pitch:.2f} mm) is too coarse to thicken {min_mm:g} mm features"}
    ri = int(math.ceil(r))
    ball = _ball(ri)
    opened = ndimage.binary_opening(g.v, structure=ball)
    thin = g.v & ~opened
    if not thin.any():
        return {"thickened": 0}
    touching = thin & ndimage.binary_dilation(opened, structure=np.ones((3, 3, 3), bool))
    labels, n = ndimage.label(thin, structure=np.ones((3, 3, 3), bool))
    idx = np.arange(1, n + 1)
    size = ndimage.sum(thin, labels, idx)
    contact = ndimage.sum(touching, labels, idx)
    boxes = ndimage.find_objects(labels)
    chosen = []
    for i, box in enumerate(boxes):
        if box is None:
            continue
        length = max(s.stop - s.start for s in box)
        # stick-outs: long enough to be a feature, and mostly not glued along the hull
        if length >= 2 * ri and size[i] >= 4 and contact[i] / size[i] < 0.35 and _rod_like(labels[box] == i + 1, ri):
            chosen.append(i + 1)
    if not chosen:
        return {"thickened": 0}
    for lab in chosen:
        box = boxes[lab - 1]
        sl = tuple(slice(max(b.start - ri - 1, 0), min(b.stop + ri + 1, dim)) for b, dim in zip(box, g.shape))
        comp = labels[sl] == lab
        axis = skeletonize(comp).astype(bool)
        if axis.sum() < max(2, comp.sum() // (4 * ri * ri + 1)):
            axis = comp  # a feature only a voxel or two wide: it already is its own axis
        g.v[sl] |= ndimage.distance_transform_edt(~axis) <= r
    log(f"  thickened {len(chosen)} thin features to {min_mm:g} mm")
    return {"thickened": len(chosen)}


def _rod_like(comp: np.ndarray, ri: int) -> bool:
    """True for rod-shaped features (antennas, barrels, pitot tubes). Sheets (thin wings,
    fins, panels) are left alone: growing them about their voxel skeleton raised
    zig-zag ridges across their surface."""
    pts = np.argwhere(comp).astype(np.float64)
    if len(pts) < 4:
        return True
    spread = np.sqrt(np.maximum(np.linalg.eigvalsh(np.cov(pts.T))[::-1], 0))
    return spread[1] <= max(1.5 * ri, 0.2 * spread[0])

def hollow(g: Grid, wall_mm: float, keep_solid: np.ndarray | None = None) -> None:
    from scipy import ndimage
    n = max(1, int(round(wall_mm / g.pitch)))
    inner = ndimage.binary_erosion(g.v, structure=_ball(1), iterations=n)
    if keep_solid is not None:
        inner &= ~keep_solid
    g.v &= ~inner


# --- stand ----------------------------------------------------------------------

@dataclass
class StandSpec:
    x: float              # post position (model coords, mm)
    y: float
    top_z: float          # where the post meets the model's underside
    post_r: float
    peg_r: float
    peg_depth: float
    base_r: float
    base_t: float = 4.0
    clearance: float = 0.15
    shape: str = "round"   # round | rect
    base_w: float = 0.0     # rect: width (x) and depth (y) of the base
    base_d: float = 0.0
    text: str = ""
    text2: str = ""         # rect: second, smaller line
    logo: str = ""          # rect: path of a logo mask (white = raised)
    text_band: float = 0.0  # rect: depth of the front strip that carries the text
    mouth_z: float | None = None  # lowest point of the underside around the socket (sloped
                                  # undersides: top_z is the highest, so the socket is full depth)
    posts: list = field(default_factory=list)   # more posts: (x, y, top_z, mouth_z) each
    balance: float | None = None  # mm the model's balance point sits inside the base edge
                                  # (negative: outside, it would tip over)


def all_posts(s: StandSpec) -> list[tuple[float, float, float, float | None]]:
    """Every post: (x, y, top_z, mouth_z), the first one is the spec's own."""
    return [(s.x, s.y, s.top_z, s.mouth_z)] + [tuple(p) for p in s.posts]


def plan_stand(g: Grid, opts: PrintOptions, log=print) -> StandSpec:
    """Pick where the post meets the model: under the center of mass (moved by the
    stand_forward_mm / stand_side_mm offsets), where the model is thick enough to take the
    peg socket and the underside is broad, not the edge of a thin fin or antenna."""
    from scipy import ndimage
    size = opts.size_mm
    post_r = float(np.clip(size * 0.03, 3.0, 8.0))
    peg_r = post_r * 0.55
    peg_depth = float(np.clip(size * 0.05, 4.0, 10.0))
    com = np.array(ndimage.center_of_mass(g.v))
    any_z = g.v.any(axis=2)
    low = np.where(any_z, np.argmax(g.v, axis=2), -1)
    # solid run upwards from the lowest voxel of each column
    run = np.zeros(any_z.shape, int)
    cols = np.argwhere(any_z)
    if len(cols) == 0:
        raise ValueError("Nothing to put on a stand")
    # Evaluate a window of columns around the center of mass (cheap and stable).
    rad = int(math.ceil(max(g.shape[:2]) * 0.25))
    # model axes after posing (NOSE_RIGHT): +x is the nose, -y the right side
    cx = int(np.clip(com[0] + opts.stand_forward_mm / g.pitch, 0, g.shape[0] - 1))
    cy = int(np.clip(com[1] - opts.stand_side_mm / g.pitch, 0, g.shape[1] - 1))
    # how far the underside rises within the post's footprint: small on a broad belly,
    # large under a thin fin or keel (the post would only touch its edge)
    rp = max(1, int(round(post_r / g.pitch)))
    yy, xx = np.ogrid[-rp:rp + 1, -rp:rp + 1]
    foot = xx * xx + yy * yy <= rp * rp
    lowf = np.where(any_z, low, g.shape[2]).astype(np.int32)
    rise = ndimage.maximum_filter(lowf, footprint=foot, mode="constant", cval=g.shape[2]) - lowf
    # and whether anything hangs lower inside the post's path (a fin beside a flat belly:
    # the post drops straight down through it)
    rc = rp + max(1, int(round(1.0 / g.pitch)))
    yy, xx = np.ogrid[-rc:rc + 1, -rc:rc + 1]
    path = xx * xx + yy * yy <= rc * rc
    drop = lowf - ndimage.minimum_filter(lowf, footprint=path, mode="constant", cval=g.shape[2])
    need = int(math.ceil((peg_depth + 1.0) / g.pitch))
    side_w = 1.0 if opts.figure else 3.0
    front_w = 0.0 if opts.figure else 1.0

    def pick(cx: int, cy: int, rad: int) -> tuple[int, int]:
        best, best_score = None, -1e18
        step = max(1, rad // 40)
        for i in range(max(cx - rad, 0), min(cx + rad + 1, g.shape[0]), step):
            for j in range(max(cy - rad, 0), min(cy + rad + 1, g.shape[1]), step):
                if low[i, j] < 0:
                    continue
                col = g.v[i, j, low[i, j]:]
                gap = np.argmin(col) if not col.all() else len(col)
                run[i, j] = gap
                # sideways moves count triple: a fin under the middle pushes the peg in front
                # of or behind it on the centerline before off to one side, where the fin
                # hides the post from the stand's front (figures face the front: no
                # preferred direction)
                d = math.hypot(i - cx, (j - cy) * side_w) * g.pitch
                # prefer a broad part of the underside (a shallower socket in the belly beats
                # a deep one up a fin), then columns that fit the socket, then close to the
                # target, then low
                steep = rise[i, j] * g.pitch > post_r or drop[i, j] * g.pitch > 1.0
                score = (1e6 if gap >= need else gap * 100) - (3e6 if steep else 0) \
                    - d * 10 - low[i, j] * g.pitch + (cy - j) * g.pitch * front_w  # ties: the front side
                if score > best_score:
                    best, best_score = (i, j), score
        if best is None:
            raise ValueError("Nothing to put on a stand")
        return best

    def mouth(i: int, j: int) -> tuple[float, float]:
        # On a sloped underside (an incline or tilt) the socket's far edge is higher than
        # its near edge: measure the depth from the highest point of the mouth, and open
        # the mouth down to the lowest, or a 45 degree tilt leaves a scoop ~1 mm deep.
        rv = int(math.ceil((peg_r + 0.15) / g.pitch))
        ii, jj = np.ogrid[-rv:rv + 1, -rv:rv + 1]
        win = low[max(i - rv, 0):i + rv + 1, max(j - rv, 0):j + rv + 1]
        disk = (ii * ii + jj * jj <= rv * rv)[max(rv - i, 0):, max(rv - j, 0):][:win.shape[0], :win.shape[1]]
        ring = win[disk & (win >= 0)]
        return (g.origin[2] + (ring.max() if len(ring) else low[i, j]) * g.pitch,
                g.origin[2] + (ring.min() if len(ring) else low[i, j]) * g.pitch)

    xs_occ = np.nonzero(any_z.any(axis=1))[0]
    length = (xs_occ[-1] - xs_occ[0] + 1) * g.pitch if len(xs_occ) else 0.0
    if opts.stand_posts >= 2 and length > 8 * post_r and not opts.figure:
        # fore and aft of the balance point (as moved), each its own pick
        half = int(round(0.22 * length / g.pitch))
        cells = [pick(int(np.clip(cx + k * half, 0, g.shape[0] - 1)), cy, max(2, rad // 2)) for k in (-1, 1)]
    else:
        cells = [pick(cx, cy, rad)]
    fit = min(run[i, j] for i, j in cells) * g.pitch
    if fit < peg_depth + 1.0:
        peg_depth = max(2.0, fit - 1.0)
        log(f"  the model is thin under the post: socket depth limited to {peg_depth:.1f} mm")
    found = []
    for i, j in cells:
        top_z, mouth_z = mouth(i, j)
        found.append((g.origin[0] + (i + 0.5) * g.pitch, g.origin[1] + (j + 0.5) * g.pitch, top_z, mouth_z))
    # the first post is the one the base hangs from (the lowest underside: the shortest post)
    found.sort(key=lambda p: -p[2])
    x, y, top_z, mouth_z = found[0]
    lo = g.origin
    hi = g.origin + np.array(g.shape) * g.pitch
    base_r = float(np.clip(0.3 * max(hi[0] - lo[0], hi[1] - lo[1]), 25.0, 150.0))
    spec = StandSpec(x, y, top_z, post_r, peg_r, peg_depth, base_r, mouth_z=mouth_z,
                     posts=[list(p) for p in found[1:]])
    spread = max(p[0] for p in found) - min(p[0] for p in found)
    if opts.stand_shape == "rect":
        _plan_rect(spec, hi[0] - lo[0], hi[1] - lo[1], opts.stand_text, opts.stand_text2, opts.stand_logo,
                   opts.stand_base_w_mm, opts.stand_base_d_mm, spread)
    else:
        spec.base_r = max(spec.base_r, spread / 2 + post_r + 10.0)
        if opts.stand_base_w_mm > 0:
            spec.base_r = max(opts.stand_base_w_mm / 2, spread / 2 + post_r + 2.0)
    # will it stand? the model's balance point against the base's edge
    cm = g.origin[:2] + (com[:2] + 0.5) * g.pitch
    x0, x1, y0, y1 = _base_box(spec)
    if spec.shape == "rect":
        spec.balance = float(min(cm[0] - x0, x1 - cm[0], cm[1] - y0, y1 - cm[1]))
    else:
        bx, by = (x0 + x1) / 2, (y0 + y1) / 2
        spec.balance = float(spec.base_r - math.hypot(cm[0] - bx, cm[1] - by))
    if spec.balance < 0:
        log(f"  the model's balance point is {-spec.balance:.0f} mm outside the base: it would tip over; "
            "make the base bigger or move the peg")
    return spec


def _plan_rect(s: StandSpec, model_w: float, model_d: float, text: str,
               text2: str = "", logo: str = "", base_w: float = 0.0, base_d: float = 0.0,
               spread: float = 0.0) -> None:
    """Rectangular plinth about half the model's footprint. With a plate (text, a second
    line and/or a logo), its front edge is bevelled back at PLATE_ANGLE and the plate
    stands out of that sloped face, so it reads when you look at the stand from the
    front. The post stays behind the bevel."""
    s.shape = "rect"
    s.base_w = float(np.clip(0.55 * model_w, 50.0, 400.0))
    s.base_d = float(np.clip(0.4 * model_d, 35.0, 300.0))
    if base_w > 0:
        s.base_w = float(base_w)
    if base_d > 0:
        s.base_d = float(base_d)
    s.base_w = max(s.base_w, spread + 2 * s.post_r + 10.0)   # both posts on the base
    s.text = (text or "").strip()[:60]
    s.text2 = (text2 or "").strip()[:60]
    s.logo = logo if logo and Path(logo).is_file() else ""
    if _has_plate(s):
        t = 0.18 * s.base_d
        if s.text and s.text2:
            t *= 1.3  # two lines need a taller face
        s.base_t = float(np.clip(t, 9.0, 20.0 if s.text2 else 16.0))   # tall enough to read
        s.text_band = _bevel_run(s) + 4.0
    else:
        s.base_t, s.text_band = 5.0, 0.0


def _has_plate(s: StandSpec) -> bool:
    return s.shape == "rect" and bool(s.text or s.text2 or s.logo)


PLATE_ANGLE = 55.0        # degrees from horizontal: steep enough to read from the front
PLATE_FOOT = 1.5          # the bevel starts this high above the bed (keeps a crisp first layer)
TEXT_RELIEF_MM = 0.8


def _bevel_run(s: StandSpec) -> float:
    """Horizontal depth of the beveled front face."""
    return (s.base_t - PLATE_FOOT) / math.tan(math.radians(PLATE_ANGLE))


def _base_box(s: StandSpec):
    """(x0, x1, y0, y1) of the base in model coordinates, centered on the posts."""
    ps = all_posts(s)
    cx = sum(p[0] for p in ps) / len(ps)
    cy = sum(p[1] for p in ps) / len(ps)
    if s.shape == "rect":
        y_back = cy + (s.base_d - s.text_band) / 2
        return cx - s.base_w / 2, cx + s.base_w / 2, y_back - s.base_d, y_back
    return cx - s.base_r, cx + s.base_r, cy - s.base_r, cy + s.base_r


def _plate_text(s: StandSpec):
    """The plate rasterised once: (bool image, px per mm, u0, v_top) where u runs along
    x and v up the bevelled face; None without a plate. Layout: the logo (if any) on
    the left at the face's full height, then the text: one line, or two with the
    second about 70% the size of the first, each centered."""
    if not _has_plate(s):
        return None
    from PIL import Image, ImageDraw, ImageFont
    x0, x1, _, _ = _base_box(s)
    face_len = (s.base_t - PLATE_FOOT) / math.sin(math.radians(PLATE_ANGLE))
    margin_u, margin_v = 3.0, 1.2
    box_w, box_h = (x1 - x0) - 2 * margin_u, face_len - 2 * margin_v
    px_mm = 20.0  # render at 20 px per mm, then sample at the grid's resolution
    W, H = int(box_w * px_mm), max(1, int(box_h * px_mm))

    def font(size):
        for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    logo = None
    if s.logo:
        m = Image.open(s.logo).convert("L")
        lw = max(1, int(round(m.width * H / max(m.height, 1))))
        lw = min(lw, W // 3 if (s.text or s.text2) else W)
        lh = max(1, int(round(m.height * lw / max(m.width, 1))))
        logo = m.resize((lw, lh), Image.LANCZOS)
    gap = int(0.5 * px_mm * 4) if logo is not None and (s.text or s.text2) else 0
    text_w = W - (logo.width if logo is not None else 0) - gap

    lines = [t for t in (s.text, s.text2) if t]
    rendered = []
    if lines and text_w > px_mm:
        ratios = [1.0, 0.7][:len(lines)] if s.text else [1.0]
        spacing = 0.18
        size = max(8, int(H / (sum(ratios) + spacing * (len(lines) - 1))))
        while True:
            fonts = [font(max(6, int(size * r))) for r in ratios]
            boxes = [f.getbbox(t) for f, t in zip(fonts, lines)]
            widths = [bx[2] - bx[0] for bx in boxes]
            heights = [bx[3] - bx[1] for bx in boxes]
            total_h = sum(heights) + int(spacing * size) * (len(lines) - 1)
            if (max(widths) <= text_w and total_h <= H) or size <= 8:
                break
            size = int(size * 0.94)
        for f, t, bx in zip(fonts, lines, boxes):
            im = Image.new("L", (max(1, bx[2] - bx[0]), max(1, bx[3] - bx[1])), 0)
            ImageDraw.Draw(im).text((-bx[0], -bx[1]), t, fill=255, font=f)
            rendered.append(im)
        block_w = max(im.width for im in rendered)
        block_h = sum(im.height for im in rendered) + int(spacing * size) * (len(rendered) - 1)
    else:
        block_w = block_h = 0

    total_w = (logo.width if logo is not None else 0) + gap + block_w
    img = Image.new("L", (max(1, total_w), H), 0)
    lab = np.zeros((H, max(1, total_w)), np.uint8)   # 1 logo, 2 first line, 3 second line
    x = 0
    if logo is not None:
        img.paste(logo, (0, (H - logo.height) // 2))
        y_l = (H - logo.height) // 2
        lab[y_l:y_l + logo.height, :logo.width][np.asarray(logo) > 127] = 1
        x = logo.width + gap
    y = (H - block_h) // 2
    line_ids = [2, 3] if s.text else [3]
    for im, k in zip(rendered, line_ids):
        px = x + (block_w - im.width) // 2
        img.paste(im, (px, y), im)
        lab[y:y + im.height, px:px + im.width][np.asarray(im) > 127] = k
        y += im.height + int(0.18 * size)
    a = np.asarray(img) > 127
    u0 = (x0 + x1) / 2 - a.shape[1] / px_mm / 2           # centered along the plate
    v_top = face_len / 2 + a.shape[0] / px_mm / 2          # centered up the face
    return a, px_mm, u0, v_top, lab


def _plate_slice(s: StandSpec, X, Y, z: float, text) -> np.ndarray:
    """Raised letters on the bevelled face, for one horizontal slice."""
    a, px_mm, u0, v_top = text[:4]
    th = math.radians(PLATE_ANGLE)
    _, _, y0, _ = _base_box(s)
    # bevel plane through (y0, PLATE_FOOT), rising backwards; n points out (forward and up)
    dy, dz = Y - y0, z - PLATE_FOOT
    d = -dy * math.sin(th) + dz * math.cos(th)             # height above the face
    v = dy * math.cos(th) + dz * math.sin(th)              # distance up the face
    col = np.floor((X - u0) * px_mm).astype(int)
    row = np.floor((v_top - v) * px_mm).astype(int)
    ok = (d >= -0.05) & (d < TEXT_RELIEF_MM) & (col >= 0) & (col < a.shape[1]) & (row >= 0) & (row < a.shape[0])
    out = np.zeros(X.shape, bool)
    out[ok] = a[row[ok], col[ok]]
    return out


def _stand_slice(X, Y, z: float, s: StandSpec, height: float, peg: bool, text) -> np.ndarray:
    """One horizontal slice (2D X/Y, height z above the base bottom) of base + post (+ peg)."""
    bx0, bx1, by0, by1 = _base_box(s)
    r = np.hypot(X - (bx0 + bx1) / 2, Y - (by0 + by1) / 2)   # round base: from its center
    ch = min(1.5, s.base_t / 2)
    inset = max(ch - z, 0.0) + max(z - (s.base_t - ch), 0.0)  # 45 degree chamfers
    occ = np.zeros(X.shape, bool)
    if 0 <= z < s.base_t:
        if s.shape == "rect":
            x0, x1, y0, y1 = _base_box(s)
            front = Y - y0
            if text is not None:  # the bevelled plate replaces the front chamfer
                front = front - max(z - PLATE_FOOT, 0.0) / math.tan(math.radians(PLATE_ANGLE)) + inset
            occ |= np.minimum(np.minimum(X - x0, x1 - X), np.minimum(front, y1 - Y)) > inset
        else:
            occ |= r < s.base_r - inset
    if text is not None and PLATE_FOOT <= z < s.base_t + TEXT_RELIEF_MM:
        occ |= _plate_slice(s, X, Y, z, text)
    for px, py, ptz, _ in all_posts(s):
        rr = np.hypot(X - px, Y - py)
        post_top = s.base_t + height + (ptz - s.top_z)
        if 0 <= z < post_top:
            occ |= rr < s.post_r
        if peg and 0 <= z < post_top + s.peg_depth - 0.4:
            occ |= rr < s.peg_r - s.clearance
    return occ


def _stand_occupancy(X, Y, Z, s: StandSpec, height: float, peg: bool) -> np.ndarray:
    """Base (round disc or rectangular plinth with text plate) + post (+ peg) on a 3D grid
    whose X/Y/Z come from meshgrid(..., indexing="ij"); base bottom at z=0."""
    X2, Y2, zs = X[:, :, 0], Y[:, :, 0], Z[0, 0, :]
    text = _plate_text(s)
    return np.stack([_stand_slice(X2, Y2, float(z), s, height, peg, text) for z in zs], axis=2)


def make_stand_mesh(s: StandSpec, height: float, pitch: float, peg: bool, smooth: bool,
                    preview: bool = False) -> trimesh.Trimesh:
    """The stand, meshed from its exact distance field: flat faces and bevel, round post
    and peg, and lettering/logo with smooth edges. preview: a coarser mesh in seconds,
    for the viewer."""
    from skimage.measure import marching_cubes
    if preview:
        pitch = 0.3 if _has_plate(s) else 0.6
    else:
        pitch = min(pitch, 0.12 if _has_plate(s) else 0.2)
    x0, x1, y0, y1 = _base_box(s)
    lo = np.array([x0 - 2, y0 - 2, -1.0])
    hi = np.array([x1 + 1, y1 + 1, s.base_t + height + s.peg_depth + 1])
    pitch = max(pitch, (float(np.prod(hi - lo)) / 2.5e8) ** (1 / 3))  # memory cap for big stands
    xs = np.arange(lo[0], hi[0] + pitch, pitch)
    ys = np.arange(lo[1], hi[1] + pitch, pitch)
    zs = np.arange(lo[2], hi[2] + pitch, pitch)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    text = _plate_text(s)
    tsdf = _plate_sdf(text)
    field = np.empty((len(xs), len(ys), len(zs)), np.float32)
    for k, z in enumerate(zs):
        field[:, :, k] = _stand_sdf(X, Y, float(z), s, height, peg, text, tsdf)
    field = np.pad(field, 1, constant_values=np.float32(pitch * 4))
    verts, faces, _, _ = marching_cubes(field, level=0.0, spacing=(pitch, pitch, pitch))
    verts += lo - pitch
    mesh = trimesh.Trimesh(verts, faces, process=False)
    mesh = _drop_small_pieces(mesh, MIN_PIECE_FACES)
    mesh.fix_normals()
    return mesh


def _plate_sdf(text):
    """2D signed distance (mm, negative inside the letters) over the plate image, for
    smooth letter edges; None without a plate."""
    if text is None:
        return None
    from scipy import ndimage
    a, px_mm = text[0], text[1]
    a = np.pad(a, 2)
    inside = ndimage.distance_transform_edt(a)
    outside = ndimage.distance_transform_edt(~a)
    return ((outside - inside) / px_mm).astype(np.float32)


def _stand_sdf(X, Y, z: float, s: StandSpec, height: float, peg: bool, text, tsdf) -> np.ndarray:
    """Signed distance (mm, negative inside) of base + post (+ peg) (+ plate lettering)
    at height z above the base bottom, over a 2D X/Y slice."""
    from scipy import ndimage
    bx0, bx1, by0, by1 = _base_box(s)
    r = np.hypot(X - (bx0 + bx1) / 2, Y - (by0 + by1) / 2)   # round base: from its center
    ch = min(1.5, s.base_t / 2)
    h = np.float32(1 / math.sqrt(2))
    zt, zb = z - s.base_t, -z
    if s.shape == "rect":
        x0, x1, y0, y1 = _base_box(s)
        sides = [x0 - X, X - x1, Y - y1]
        if text is not None:  # bevelled plate face, vertical below PLATE_FOOT
            th = math.radians(PLATE_ANGLE)
            front = np.maximum(y0 - Y, -(Y - y0) * math.sin(th) + (z - PLATE_FOOT) * math.cos(th))
            top_edges = sides
        else:
            front = y0 - Y
            top_edges = sides + [front]
        d = np.maximum.reduce([*sides, front, np.full_like(X, zt), np.full_like(X, zb)])
        for e in top_edges:  # 45 degree chamfers on the top edges
            d = np.maximum(d, (e + zt + ch) * h)
        for e in [*sides, y0 - Y]:  # and on the bottom edges
            d = np.maximum(d, (e + zb + ch) * h)
    else:
        e = r - s.base_r
        d = np.maximum.reduce([e, np.full_like(X, zt), np.full_like(X, zb), (e + zt + ch) * h, (e + zb + ch) * h])
    for px, py, ptz, _ in all_posts(s):
        rr = np.hypot(X - px, Y - py)
        post_top = s.base_t + height + (ptz - s.top_z)
        d = np.minimum(d, np.maximum(rr - s.post_r, np.float32(max(-z, z - post_top))))
        if peg:
            pr, ptop = s.peg_r - s.clearance, post_top + s.peg_depth - 0.4
            pd = np.maximum(rr - pr, np.float32(max(post_top - 1 - z, z - ptop)))
            pd = np.maximum(pd, (rr - pr + z - ptop + 0.4) * h)  # chamfered tip finds the socket
            d = np.minimum(d, pd)
    if text is not None and PLATE_FOOT - 1 <= z < s.base_t + TEXT_RELIEF_MM + 1:
        a, px_mm, u0, v_top = text[:4]
        th = math.radians(PLATE_ANGLE)
        _, _, y0, _ = _base_box(s)
        dy, dz = Y - y0, z - PLATE_FOOT
        dface = -dy * math.sin(th) + dz * math.cos(th)         # height above the face
        v = dy * math.cos(th) + dz * math.sin(th)              # distance up the face
        col = (X - u0) * px_mm + 2 - 0.5                       # +2: _plate_sdf's padding
        row = (v_top - v) * px_mm + 2 - 0.5
        t2 = ndimage.map_coordinates(tsdf, [row.ravel(), col.ravel()], order=1, mode="constant",
                                     cval=10.0).reshape(X.shape)
        letters = np.maximum.reduce([t2, dface - TEXT_RELIEF_MM, -dface - 0.6])
        d = np.minimum(d, letters)
    return d.astype(np.float32)


STAND_PARTS = ("base", "logo", "text", "text2")   # stand face labels 0..3


def stand_face_labels(verts: np.ndarray, faces: np.ndarray, s: StandSpec, base_bottom_z: float,
                      height: float) -> np.ndarray:
    """Which part of the stand each face is: 0 base and posts, 1 logo, 2 the plate's first
    line, 3 its second line; -1 not the stand (an attached stand's model faces). verts in
    the stand's model coordinates; base_bottom_z is where the base sits."""
    c = np.asarray(verts, float)[np.asarray(faces, int)].mean(axis=1)
    X, Y, Z = c[:, 0], c[:, 1], c[:, 2] - base_bottom_z
    lab = np.full(len(c), -1, np.int8)
    x0, x1, y0, y1 = _base_box(s)
    top = s.base_t + TEXT_RELIEF_MM + 0.3
    if s.shape == "rect":
        lab[(X >= x0 - 0.5) & (X <= x1 + 0.5) & (Y >= y0 - 0.5) & (Y <= y1 + 0.5) & (Z <= top)] = 0
    else:
        lab[(np.hypot(X - (x0 + x1) / 2, Y - (y0 + y1) / 2) <= s.base_r + 0.5) & (Z <= top)] = 0
    for px, py, ptz, _ in all_posts(s):
        ptop = s.base_t + height + (ptz - s.top_z) + s.peg_depth
        lab[(np.hypot(X - px, Y - py) <= s.post_r + 0.4) & (Z >= -0.5) & (Z <= ptop)] = 0
    text = _plate_text(s)
    if text is not None:
        from scipy import ndimage
        a, px_mm, u0, v_top, img = text
        img = ndimage.maximum_filter(img, size=5)   # faces on a letter's edge still find it
        th = math.radians(PLATE_ANGLE)
        dy, dz = Y - y0, Z - PLATE_FOOT
        dface = -dy * math.sin(th) + dz * math.cos(th)
        v = dy * math.cos(th) + dz * math.sin(th)
        col = np.floor((X - u0) * px_mm).astype(int)
        row = np.floor((v_top - v) * px_mm).astype(int)
        on = (dface > 0.15) & (dface < TEXT_RELIEF_MM + 0.4) & (col >= 0) & (col < img.shape[1]) \
            & (row >= 0) & (row < img.shape[0])
        k = np.zeros(len(c), np.int8)
        k[on] = img[row[on], col[on]]
        lab[on & (k > 0)] = k[on & (k > 0)]
    return lab


def stand_filaments(opts: "PrintOptions", colors: list[str]) -> tuple[list[str], dict[int, int]]:
    """The filament list with the stand's colors added (a color already in the list is
    reused), and stand part -> filament number. Unset: the base takes filament 1, the
    logo and text the base's."""
    colors = list(colors)

    def slot(hex_: str) -> int:
        hex_ = hex_.strip().upper()
        for i, c in enumerate(colors):
            if c.upper() == hex_:
                return i + 1
        colors.append(hex_)
        return len(colors)
    want = [opts.stand_base_color, opts.stand_logo_color, opts.stand_text_color, opts.stand_text2_color]
    base = slot(want[0]) if want[0] else 1
    out = {0: base}
    for k in (1, 2, 3):
        out[k] = slot(want[k]) if want[k] else base
    return colors, out


def cut_socket(g: Grid, s: StandSpec) -> None:
    X, Y = np.meshgrid(g.centers(0), g.centers(1), indexing="ij")
    z = g.centers(2)
    r = s.peg_r + s.clearance
    for px, py, ptz, pmz in all_posts(s):
        disk = np.hypot(X - px, Y - py) < r
        mouth = ptz if pmz is None else min(pmz, ptz)
        zmask = (z >= mouth - g.pitch) & (z < ptz + s.peg_depth)
        g.v[disk[:, :, None] & zmask[None, None, :]] = False
        bottom, top = mouth - 3.0, ptz + s.peg_depth
        g.cut_sdf.append(lambda X, Y, z, px=px, py=py, bottom=bottom, top=top:
                         np.maximum(np.hypot(X - px, Y - py) - r, np.float32(max(bottom - z, z - top))))


def socket_reinforcement(g: Grid, s: StandSpec, wall_mm: float) -> np.ndarray:
    X, Y = np.meshgrid(g.centers(0), g.centers(1), indexing="ij")
    z = g.centers(2)
    keep = np.zeros(g.shape, bool)
    for px, py, ptz, _ in all_posts(s):
        disk = np.hypot(X - px, Y - py) < s.peg_r + s.clearance + wall_mm + 1.0
        zmask = z < ptz + s.peg_depth + wall_mm + 1.0
        keep |= disk[:, :, None] & zmask[None, None, :]
    return keep


def fuse_stand(g: Grid, s: StandSpec, height: float) -> Grid:
    """Attached stand: extend the grid downward and add base + post into the model."""
    below = int(math.ceil((height + s.base_t + 1) / g.pitch))
    x0, x1, y0, y1 = _base_box(s)
    reach = max(s.x - x0, x1 - s.x, s.y - y0, y1 - s.y)
    grow = int(math.ceil(reach / g.pitch))
    new = Grid.__new__(Grid)
    new.pitch = g.pitch
    new.origin = g.origin - g.pitch * np.array([grow, grow, below])
    new.shape = (g.shape[0] + 2 * grow, g.shape[1] + 2 * grow, g.shape[2] + below)
    new.v = np.zeros(new.shape, bool)
    new.v[grow:grow + g.shape[0], grow:grow + g.shape[1], below:] = g.v
    X, Y, Z = np.meshgrid(new.centers(0), new.centers(1), new.centers(2), indexing="ij")
    base_bottom = s.top_z - height - s.base_t
    # post reaches into the model by the socket depth so the joint is solid
    occ = _stand_occupancy(X, Y, Z - base_bottom, s, height + s.peg_depth * 0.6, peg=False)
    new.v |= occ
    new.add_sdf = list(g.add_sdf)
    new.cut_sdf = list(g.cut_sdf)
    text = _plate_text(s)
    tsdf = _plate_sdf(text)
    post_h = height + s.peg_depth * 0.6
    new.add_sdf.append(lambda X, Y, z: _stand_sdf(X, Y, z - base_bottom, s, post_h, False, text, tsdf))
    return new


# --- output ---------------------------------------------------------------------

def repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fill_holes(mesh)
    return mesh


def decimate(mesh: trimesh.Trimesh, max_faces: int, log=print) -> trimesh.Trimesh:
    if len(mesh.faces) <= max_faces:
        return mesh
    try:
        out = mesh.simplify_quadric_decimation(face_count=max_faces)
        # simplification can leave a few two-triangle slivers that fail watertight checks
        out = _drop_small_pieces(out, 8)
        log(f"  reduced {len(mesh.faces):,} -> {len(out.faces):,} triangles")
        return out
    except Exception as e:  # fast-simplification missing or failed
        log(f"  could not reduce triangle count ({e}); keeping full mesh")
        return mesh


def _place_on_bed(mesh: trimesh.Trimesh, x_offset: float = 0.0) -> None:
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2 + x_offset, -(lo[1] + hi[1]) / 2, -lo[2]])


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:60].strip("_") or "part"


def write_bodies(bodies: list[tuple[str, trimesh.Trimesh]], out: Path, fmt: str) -> list[str]:
    """One file holding every body (3MF objects / OBJ groups), or one file per body (STL/PLY)."""
    if len(bodies) == 1:
        bodies[0][1].export(out, file_type=fmt)
        return [str(out)]
    if fmt in ("3mf", "obj"):
        scene = trimesh.Scene()
        for i, (n, m) in enumerate(bodies):
            scene.add_geometry(m, node_name=f"{i:03d}_{n}"[:60], geom_name=f"{i:03d}_{n}"[:60])
        scene.export(out, file_type=fmt)
        return [str(out)]
    folder = out.with_suffix("")
    folder.mkdir(exist_ok=True)
    files = []
    for i, (n, m) in enumerate(bodies):
        p = folder / f"{i:03d}_{_safe(n)}.{fmt}"
        m.export(p, file_type=fmt)
        files.append(str(p))
    return files


def prepare(glb: Path, out: Path, opts: PrintOptions, log=print) -> dict:
    glb, out = Path(glb), Path(out)
    fmt = opts.format.lower()
    if fmt not in PRINT_FORMATS:
        raise ValueError(f"format must be one of {', '.join(PRINT_FORMATS)}")
    if opts.mode not in ("solid", "parts"):
        raise ValueError("mode must be solid or parts")
    if opts.stand_shape not in ("round", "rect"):
        raise ValueError("stand_shape must be round or rect")
    if opts.stand not in ("none", "peg", "attached"):
        raise ValueError("stand must be none, peg or attached")
    out = out.with_suffix("." + fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    min_mm = opts.min_feature_mm

    parts = load_parts(glb, opts.exclude_nodes)
    log(f"  {len(parts)} parts loaded")
    real = real_size(parts)
    scale = _orient_and_scale(parts, opts.size_mm, opts.tilt_deg, opts.scale, opts.roll_deg,
                              not opts.figure)
    if opts.scale > 0:  # stand and peg sizes follow the printed size
        lo, hi = _bounds(parts)
        opts = replace(opts, size_mm=float(np.max(hi - lo)))
        log(f"  1:{round(opts.scale, 1):g} makes the longest side {opts.size_mm:.0f} mm")
    if opts.stand_scale and opts.stand != "none" and opts.stand_shape == "rect":
        # the scale goes on the plate's second line, after any text already there
        line2 = "   ".join(t for t in (opts.stand_text2.strip(), _scale_text(scale)) if t)
        opts = replace(opts, stand_text2=line2)
    parts = apply_overrides(parts, opts, log)
    report = {"source": str(glb), "mode": opts.mode, "scale": _scale_text(scale),
              "real_m": [round(x, 2) for x in real], "longest_mm": round(opts.size_mm, 1),
              "min_feature_mm": round(min_mm, 2), "stand": opts.stand}

    if opts.mode == "solid":
        g = voxelize(parts, opts, extra_mm=0.0, log=log)
        report["voxel_mm"] = round(g.pitch, 3)
        if opts.thicken and min_mm > 0:
            report.update(thicken_thin_features(g, min_mm, log))
        report["joined"] = connect_pieces(g, min_mm, max(JOIN_GAP_MM, JOIN_GAP_SHARE * opts.size_mm), log)
        report["loose_removed"] = drop_loose_bits(g, log)
        spec = plan_stand(g, opts, log) if opts.stand != "none" else None
        if spec is not None:
            report["posts"] = len(all_posts(spec))
            report["balance_mm"] = None if spec.balance is None else round(spec.balance, 1)
        stand_h = opts.stand_height_mm
        if spec and opts.figure:
            # A figure stands on the base: the base top sits at its lowest point (the
            # feet) and the post rises from there to where it meets the figure.
            zs = np.nonzero(g.v.any(axis=(0, 1)))[0]
            feet_z = g.origin[2] + zs[0] * g.pitch
            stand_h = max(0.0, spec.top_z - feet_z) + (0.0 if opts.stand == "attached" else 0.2)
        if opts.hollow_mm > 0:
            keep = socket_reinforcement(g, spec, opts.hollow_mm) if spec and opts.stand == "peg" else None
            hollow(g, opts.hollow_mm, keep)
            log(f"  hollowed with {opts.hollow_mm:g} mm walls (add drain holes in your resin slicer)")
        if spec and opts.stand == "peg":
            cut_socket(g, spec)
        if spec and opts.stand == "attached":
            g = fuse_stand(g, spec, stand_h)
        mesh = drop_small_shells(decimate(g.to_mesh(opts.smooth, parts, log), opts.max_faces, log), log)
        bodies = [("model", mesh)]
        if spec and opts.stand == "peg":
            stand = make_stand_mesh(spec, stand_h, g.pitch, peg=True, smooth=opts.smooth)
            bodies.append(("stand", decimate(stand, STAND_MAX_FACES, log)))
        painted = None
        face_paint = {}
        if opts.paint_colors > 0 and fmt == "3mf":
            # before laying the bodies out: the model still sits where the original parts are
            painted = paint_faces(mesh, parts, opts.paint_colors, log)
            face_paint["model"] = painted[0]
            if spec:
                colors, part_slot = stand_filaments(opts, painted[1])
                painted = (painted[0], colors)
                lookup = np.array([part_slot[k] for k in range(4)], np.int32)
                if opts.stand == "attached":
                    base_bottom = spec.top_z - stand_h - spec.base_t
                    lab = stand_face_labels(mesh.vertices, mesh.faces, spec, base_bottom, stand_h)
                    fp = np.asarray(face_paint["model"]).copy()
                    fp[lab >= 0] = lookup[lab[lab >= 0]]
                    face_paint["model"] = fp
                elif len(bodies) > 1:
                    sm = bodies[1][1]
                    lab = stand_face_labels(sm.vertices, sm.faces, spec, 0.0, stand_h)
                    face_paint["stand"] = lookup[np.maximum(lab, 0)]
        _layout(bodies)
        if painted:
            files = [str(write_bambu_3mf(out, bodies, face_paint, painted[1], out.stem))]
            report["colors"] = painted[1]
            report["color_names"] = [closest_filament(c) for c in painted[1]]
        else:
            files = write_bodies(bodies, out, fmt)
        report.update(files=files, bodies=len(bodies),
                      triangles=sum(len(m.faces) for _, m in bodies),
                      watertight=all(m.is_watertight for _, m in bodies),
                      volume_cm3=round(sum(abs(m.volume) for _, m in bodies) / 1000, 2),
                      size_mm=[round(x, 1) for x in mesh.extents])
    else:
        meshes = []
        thickened = 0
        for n, m in parts:
            m = repair(m)
            if opts.thicken and min_mm > 0 and (opts.part_overrides.get(n) or {}).get("action", "auto") == "auto":
                if any(d["name"] == n and d["too_thin"] for d in slender_parts([(n, m)], min_mm)):
                    thickened += thicken_part(m, min_mm) > 1
            meshes.append((n, m))
        if thickened:
            log(f"  thickened {thickened} slender parts to {min_mm:g} mm")
        combined = trimesh.util.concatenate([m for _, m in meshes])
        lo, hi = combined.bounds
        offset = [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
        for _, m in meshes:
            m.apply_translation(offset)
        if opts.stand != "none":
            spec = StandSpec(0.0, 0.0, 0.0, float(np.clip(opts.size_mm * 0.03, 3, 8)), 0, 0,
                             float(np.clip(0.3 * max(hi[0] - lo[0], hi[1] - lo[1]), 25, 90)))
            stand = make_stand_mesh(spec, opts.stand_height_mm, 0.25, peg=False, smooth=opts.smooth)
            if opts.stand_shape == "rect":
                _plan_rect(spec, hi[0] - lo[0], hi[1] - lo[1], opts.stand_text, opts.stand_text2, opts.stand_logo)
                stand = make_stand_mesh(spec, opts.stand_height_mm, 0.25, peg=False, smooth=opts.smooth)
            stand.apply_translation([(hi[0] - lo[0]) / 2 + (spec.base_w / 2 if spec.shape == "rect" else spec.base_r) + 10, 0, 0])
            meshes.append(("stand", stand))
            log("  stand added as its own part (flat top: glue the model on)")
        report.update(files=write_bodies(meshes, out, fmt), bodies=len(meshes), thickened=thickened,
                      watertight_bodies=sum(m.is_watertight for _, m in meshes),
                      triangles=sum(len(m.faces) for _, m in meshes),
                      size_mm=[round(x, 1) for x in combined.extents])
    log(f"  wrote {report['files'][0]}" + (f" (+{len(report['files']) - 1} more)" if len(report["files"]) > 1 else ""))
    return report


def _layout(bodies) -> None:
    """Model centered on the bed, extra bodies (the stand) beside it on +X."""
    _place_on_bed(bodies[0][1])
    x = bodies[0][1].bounds[1][0]
    for _, m in bodies[1:]:
        w = m.extents[0]
        _place_on_bed(m, x + 10 + w / 2)
        x += 10 + w
