import math
import numpy as np
import trimesh
import pytest
from scsandbox import printprep


def make_ship_glb(path):
    """A fake 'ship' in meters, Y-up: open hull shell, wings, a collision proxy and an LOD copy."""
    scene = trimesh.Scene()
    hull = trimesh.creation.capsule(height=10, radius=1.5)
    hull.update_faces(np.arange(len(hull.faces))[20:])  # punch a hole: open shell like game meshes
    scene.add_geometry(hull, node_name="hull")
    for x in (-4, 4):
        wing = trimesh.creation.box([6, 0.05, 3])   # thin panel, zero-ish thickness
        wing.apply_translation([x, 0, 0])
        scene.add_geometry(wing, node_name=f"wing_{'l' if x < 0 else 'r'}")
    scene.add_geometry(trimesh.creation.box([30, 30, 30]), node_name="hull_collision")
    scene.add_geometry(trimesh.creation.icosphere(), node_name="hull_lod2")
    scene.export(str(path))
    return path


@pytest.fixture
def glb(tmp_path):
    return make_ship_glb(tmp_path / "ship.glb")


EXCL = [r"(?i)lod[1-9]", r"(?i)collision"]


def test_solid_is_watertight_scaled_and_on_bed(glb, tmp_path):
    opts = printprep.PrintOptions(size_mm=100, mode="solid", voxel_mm=0.5, exclude_nodes=EXCL)
    rep = printprep.prepare(glb, tmp_path / "out", opts, log=lambda *_: None)
    m = trimesh.load(rep["files"][0], force="mesh")
    assert rep["watertight"] and m.is_watertight
    assert abs(max(m.extents) - 100) < 2.5          # longest side ~ target (excluded 30m box ignored)
    assert abs(m.bounds[0][2]) < 1e-6               # sits on the bed
    assert m.extents[1] > m.extents[2]              # Y-up converted: length lies flat, not standing up
    assert rep["files"][0].endswith(".3mf")


def test_parts_stl(glb, tmp_path):
    opts = printprep.PrintOptions(size_mm=80, mode="parts", format="stl", exclude_nodes=EXCL)
    rep = printprep.prepare(glb, tmp_path / "kit", opts, log=lambda *_: None)
    assert rep["bodies"] == 3 and len(rep["files"]) == 3
    assert all(trimesh.load(f).faces.shape[0] > 0 for f in rep["files"])


def test_voxel_cap(glb, tmp_path):
    opts = printprep.PrintOptions(size_mm=100, voxel_mm=0.05, max_voxels=2_000_000, exclude_nodes=EXCL, format="stl")
    rep = printprep.prepare(glb, tmp_path / "capped", opts, log=lambda *_: None)
    assert rep["voxel_mm"] > 0.05 and rep["watertight"]


def test_everything_excluded(glb, tmp_path):
    opts = printprep.PrintOptions(exclude_nodes=[".*"])
    with pytest.raises(ValueError):
        printprep.prepare(glb, tmp_path / "x", opts, log=lambda *_: None)


def make_gunship_glb(path):
    """Box hull 10 x 4 x 2 m with a 2 cm antenna (1.5 m tall) and a 4 cm gun barrel (3 m long), Y-up."""
    scene = trimesh.Scene()
    scene.add_geometry(trimesh.creation.box([4, 2, 10]), node_name="hull")
    ant = trimesh.creation.cylinder(radius=0.01, height=1.5)          # along z -> rotate to Y-up vertical
    ant.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    ant.apply_translation([0, 1.0 + 0.75, 2])
    scene.add_geometry(ant, node_name="antenna")
    barrel = trimesh.creation.cylinder(radius=0.02, height=3)        # along z = ship length
    barrel.apply_translation([1.5, 0, 5 + 1.5 - 0.2])
    scene.add_geometry(barrel, node_name="gun_barrel")
    scene.export(str(path))
    return path


def _section_width(mesh, z, near_xy):
    """Width of the cross-section at height z closest to the given (x, y)."""
    sec = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    assert sec is not None
    best = None
    for ent in sec.discrete:
        c = ent[:, :2].mean(axis=0)
        d = np.linalg.norm(c - near_xy)
        if best is None or d < best[0]:
            best = (d, ent[:, :2].max(axis=0) - ent[:, :2].min(axis=0))
    return best[1]


def test_thin_antenna_is_thickened_hull_edges_are_not(tmp_path):
    glb = make_gunship_glb(tmp_path / "gun.glb")
    base = dict(size_mm=100, mode="solid", voxel_mm=0.2, smooth=False, format="stl")
    thin = printprep.prepare(glb, tmp_path / "a", printprep.PrintOptions(thicken=False, **base), log=lambda *_: None)
    thick = printprep.prepare(glb, tmp_path / "b", printprep.PrintOptions(nozzle_mm=0.4, min_feature_nozzles=3, **base),
                              log=lambda *_: None)
    assert thick["thickened"] >= 2 and thick["watertight"]
    m = trimesh.load(thick["files"][0], force="mesh")
    # antenna: 0.2 mm at this scale (would vanish), now at least ~1.2 mm across
    top = m.bounds[1][2] - 3
    w = _section_width(m, top, np.array([m.centroid[0], m.centroid[1]]))
    assert min(w) >= 1.0, w
    # the box hull itself barely changes (edges are not inflated)
    hull_only = trimesh.load(thin["files"][0], force="mesh")
    assert abs(m.volume - hull_only.volume) / hull_only.volume < 0.03


def test_peg_stand_and_socket(tmp_path):
    glb = make_gunship_glb(tmp_path / "gun.glb")
    opts = printprep.PrintOptions(size_mm=100, voxel_mm=0.3, stand="peg", stand_height_mm=25, format="3mf")
    rep = printprep.prepare(glb, tmp_path / "s", opts, log=lambda *_: None)
    assert rep["bodies"] == 2 and rep["watertight"]
    scene = trimesh.load(rep["files"][0], force="scene")
    meshes = sorted(scene.dump(), key=lambda m: m.volume)
    stand = meshes[0] if meshes[0].extents[2] > 25 else meshes[1]
    assert stand.extents[2] > 25 + 4                       # base + post + peg
    no_stand = printprep.prepare(glb, tmp_path / "n", printprep.PrintOptions(size_mm=100, voxel_mm=0.3),
                                 log=lambda *_: None)
    model = [m for m in meshes if m is not stand][0]
    single = trimesh.load(no_stand["files"][0], force="mesh")
    assert model.volume < single.volume - 20                # socket cut into the model (mm^3)


def test_rect_stand_with_text_plate(tmp_path):
    glb = make_gunship_glb(tmp_path / "gun.glb")
    base = dict(size_mm=100, voxel_mm=0.3, stand="peg", stand_height_mm=20, stand_shape="rect", format="3mf")
    rep = printprep.prepare(glb, tmp_path / "t", printprep.PrintOptions(**base, stand_text="GUNSHIP 01"),
                            log=lambda *_: None)
    assert rep["bodies"] == 2 and rep["watertight"]
    stand = min(trimesh.load(rep["files"][0], force="scene").dump(), key=lambda m: m.volume)
    dash = printprep.prepare(glb, tmp_path / "d", printprep.PrintOptions(**base, stand_text="-"), log=lambda *_: None)
    dash_stand = min(trimesh.load(dash["files"][0], force="scene").dump(), key=lambda m: m.volume)
    plain = printprep.prepare(glb, tmp_path / "p", printprep.PrintOptions(**base), log=lambda *_: None)
    plain_stand = min(trimesh.load(plain["files"][0], force="scene").dump(), key=lambda m: m.volume)
    ex, ey = stand.extents[:2]
    assert abs(ex - ey) > 5                                   # rectangular, not round
    assert stand.extents[2] > plain_stand.extents[2] + 3      # taller plinth to carry the bevelled plate

    def front_profile(m):  # slice halfway up the plinth's bevelled front face
        z0 = m.bounds[0][2]
        sec = m.section(plane_origin=[0, 0, z0 + 6.0], plane_normal=[0, 0, 1])
        return sec.length
    # raised letters on the bevel make the slice outline far longer than a single dash does
    assert front_profile(stand) > front_profile(dash_stand) + 10


def test_attached_stand_and_hollow(tmp_path):
    glb = make_gunship_glb(tmp_path / "gun.glb")
    rep = printprep.prepare(glb, tmp_path / "t", printprep.PrintOptions(
        size_mm=80, voxel_mm=0.4, stand="attached", stand_height_mm=20, hollow_mm=2, format="stl"),
        log=lambda *_: None)
    m = trimesh.load(rep["files"][0], force="mesh")
    assert rep["bodies"] == 1 and m.is_watertight
    assert m.extents[2] > 20 + 4                            # model raised on its post
    assert m.body_count >= 1


def test_overrides_and_analyze(tmp_path):
    glb = make_gunship_glb(tmp_path / "gun.glb")
    opts = printprep.PrintOptions(size_mm=100)
    info = printprep.analyze(glb, opts)
    names = {d["name"] for d in info["slender"]}
    assert {"antenna", "gun_barrel"} <= names
    opts = printprep.PrintOptions(size_mm=100, mode="parts", format="stl", part_overrides={
        "antenna": {"action": "remove"}, "gun_barrel": {"action": "thicken", "mm": 2.0}})
    rep = printprep.prepare(glb, tmp_path / "k", opts, log=lambda *_: None)
    assert rep["bodies"] == 2
    barrel = trimesh.load([f for f in rep["files"] if "gun_barrel" in f][0])
    assert sorted(barrel.extents)[1] >= 1.9


def test_parts_mode_auto_thickens_and_obj_ply(tmp_path):
    glb = make_gunship_glb(tmp_path / "gun.glb")
    rep = printprep.prepare(glb, tmp_path / "o", printprep.PrintOptions(size_mm=100, mode="parts", format="obj"),
                            log=lambda *_: None)
    assert rep["thickened"] == 2 and rep["files"][0].endswith(".obj")
    rep = printprep.prepare(glb, tmp_path / "p", printprep.PrintOptions(size_mm=100, voxel_mm=0.4, format="ply"),
                            log=lambda *_: None)
    assert trimesh.load(rep["files"][0]).is_watertight


def test_unused_shared_vertices_do_not_become_geometry():
    """glTF parts can share one vertex buffer; vertices a part's faces don't use must not
    turn into voxels (they used to scatter noise over the whole model)."""
    box = trimesh.creation.box([2, 2, 2])
    far = np.array([[50.0, 50.0, 50.0], [-50.0, -50.0, -50.0]])
    part = trimesh.Trimesh(np.vstack([box.vertices, far]), box.faces, process=False)
    pts = printprep._surface_points(part, 0.5)
    assert np.all(np.abs(pts) <= 1.0 + 1e-9)


def test_sloped_surfaces_are_flat_not_stair_stepped(tmp_path):
    import math
    wing = trimesh.creation.box([8, 0.4, 4])
    wing.apply_transform(trimesh.transformations.rotation_matrix(math.radians(7), [0, 0, 1]))
    trimesh.Scene({"wing": wing}).export(str(tmp_path / "w.glb"))
    rep = printprep.prepare(tmp_path / "w.glb", tmp_path / "w", printprep.PrintOptions(size_mm=60, voxel_mm=0.3, format="stl",
                            thicken=False), log=lambda *_: None)
    m = trimesh.load(rep["files"][0])
    # neighboring faces on a flat slab should be almost coplanar; voxel steps give ~20-45 degrees
    assert np.degrees(np.percentile(m.face_adjacency_angles, 90)) < 3
    assert m.is_watertight


def test_plate_second_line_and_logo(tmp_path):
    """Two lines (the second smaller) and an embossed logo beside them."""
    from PIL import Image, ImageDraw
    logo = tmp_path / "logo.png"
    im = Image.new("L", (200, 200), 0)
    ImageDraw.Draw(im).polygon([(100, 10), (190, 190), (10, 190)], fill=255)  # a solid triangle
    im.save(logo)
    s = printprep.StandSpec(0, 0, 10, 4, 2, 5, 30)
    printprep._plan_rect(s, 150, 120, "GUNSHIP 01")
    one_line_t = s.base_t
    one = printprep._plate_text(s)[0]
    printprep._plan_rect(s, 150, 120, "GUNSHIP 01", "Second line", str(logo))
    assert s.base_t > one_line_t                       # a taller face for two lines
    two = printprep._plate_text(s)[0]
    rows = np.where(two.any(axis=1))[0]
    assert two.shape[1] > one.shape[1] * 0.9 and len(rows) > 10
    # the logo sits on the left: the leftmost columns hold the triangle's wide base
    left = two[:, : two.shape[1] // 6]
    assert left.sum() > 0.2 * left.size * 0.5
    # a logo alone is a plate too
    printprep._plan_rect(s, 150, 120, "", "", str(logo))
    assert printprep._has_plate(s) and printprep._plate_text(s) is not None
    # and it all prints watertight
    glb = make_gunship_glb(tmp_path / "gun.glb")
    rep = printprep.prepare(glb, tmp_path / "t", printprep.PrintOptions(
        size_mm=100, voxel_mm=0.3, stand="peg", stand_height_mm=20, stand_shape="rect", format="3mf",
        stand_text="GUNSHIP 01", stand_text2="Second line", stand_logo=str(logo)), log=lambda *_: None)
    assert rep["bodies"] == 2 and rep["watertight"]


def test_logo_symbol_is_cut_above_the_wordmark():
    from scsandbox import logos
    m = np.zeros((100, 120), bool)
    m[5:60, 30:90] = True     # the symbol
    m[80:90, 10:110] = True   # the wordmark below it
    m[20:30, 112:118] = True  # a small mark off to the side (dropped)
    cut = logos.symbol(m)
    assert cut.shape == (55, 60) and cut.all()


def test_socket_is_full_depth_on_a_tilted_model(tmp_path, monkeypatch):
    """A tilted display angle puts the socket into a sloped underside. Its depth used to
    be measured from the lowest point of the mouth, so at 45 degrees the far side was
    only ~1 mm deep and the socket was easy to miss; the skin fitted over inner decks
    could also refill it. Now a slice part-way up the socket cuts a complete wall."""
    glb = tmp_path / "slab.glb"  # hull with an inner deck crossing the socket's column
    deck = trimesh.creation.box((3.6, 0.01, 9.6))
    deck.apply_translation([0, -0.15, 0])
    trimesh.Scene({"hull": trimesh.creation.box((4, 0.8, 10)), "deck": deck}).export(glb)
    seen = {}
    plan, layout = printprep.plan_stand, printprep._layout

    def spy_plan(g, o, log=print):
        seen["spec"] = plan(g, o, log)
        return seen["spec"]

    def spy_layout(bodies):
        before = bodies[0][1].bounds[0].copy()
        layout(bodies)
        seen["shift"] = bodies[0][1].bounds[0] - before

    monkeypatch.setattr(printprep, "plan_stand", spy_plan)
    monkeypatch.setattr(printprep, "_layout", spy_layout)
    opts = printprep.PrintOptions(size_mm=300, voxel_mm=0.5, stand="peg", tilt_deg=50, format="stl",
                                  thicken=False)
    rep = printprep.prepare(glb, tmp_path / "s", opts, log=lambda *_: None)
    model = trimesh.load(next(f for f in rep["files"] if "model" in f), force="mesh")
    sp, sh = seen["spec"], seen["shift"]
    cx, cy = sp.x + sh[0], sp.y + sh[1]
    mouth_top = sp.top_z + sh[2]  # the highest point of the underside around the socket
    r = sp.peg_r + sp.clearance
    for dz in (1.0, sp.peg_depth / 2, sp.peg_depth - 1.0):  # a complete wall all the way up
        sec = model.section(plane_origin=[cx, cy, mouth_top + dz], plane_normal=[0, 0, 1])
        d = np.hypot(sec.vertices[:, 0] - cx, sec.vertices[:, 1] - cy)
        wall = sec.vertices[np.abs(d - r) < 0.6]
        ang = np.sort(np.degrees(np.arctan2(wall[:, 1] - cy, wall[:, 0] - cx)))
        gaps = np.diff(np.concatenate([ang, ang[:1] + 360]))
        assert len(ang) > 20 and gaps.max() < 30, (dz, gaps.max() if len(ang) else None)


def _named_glb(path, parts, parent=None):
    sc = trimesh.Scene()
    for i, (name, mat) in enumerate(parts):
        box = trimesh.creation.box((1, 1, 1))
        box.apply_translation([i * 2.0, 0, 0])
        box.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(name=mat))
        under = (parent or {}).get(name)
        sc.add_geometry(box, node_name=name, geom_name=name, **({"parent_node_name": under} if under else {}))
    sc.export(path)
    return path


def test_real_helper_geometry_and_breakable_wing_sections_kept(tmp_path):
    """Hull panels hanging on helper nodes (Eclipse canopy frame, bay doors, nose panels)
    and the detachable wing sections (*_2D_dmg, *_broken) are part of the ship."""
    glb = _named_glb(tmp_path / "s.glb", [
        ("hull", "ship_paint"), ("wing_left_2D_dmg_f72800", "ship_paint"),
        ("wing_right_broken_fb38d8", "ship_paint"), ("helper_canopy_rotation_01", "ship_paint_grey"),
        ("helper_pivot", "helper_mtl"), ("hull_proxy", "ship_paint")])
    names = {n for n, _ in printprep.load_parts(glb, [r"(?i)helper", r"(?i)proxy"]) if not n.endswith("_glass")}
    assert names == {"hull", "helper_canopy_rotation_01", "wing_left_2D_dmg_f72800", "wing_right_broken_fb38d8"}, names


def test_canopy_with_glass_material_gets_no_convex_fill(tmp_path):
    glb = _named_glb(tmp_path / "c.glb", [("hull", "paint"), ("cockpit_canopy", "paint"),
                                          ("cockpit_canopy_6", "mtl_glass_exterior_012")],
                     parent={"cockpit_canopy_6": "cockpit_canopy"})
    names = [n for n, _ in printprep.load_parts(glb, [])]
    assert not any(n.endswith("_glass") for n in names), names


def test_scale_sets_the_size_and_tilt_keeps_it(glb, tmp_path):
    """1:100 of a 14 m wingspan is 140 mm, whatever the display angle."""
    real = printprep.measure(glb, EXCL)
    assert abs(real["longest_m"] - 14) < 0.1
    q = lambda *_: None  # noqa: E731
    rep = printprep.prepare(glb, tmp_path / "a", printprep.PrintOptions(scale=100, size_mm=50, mode="parts",
                            format="stl", exclude_nodes=EXCL), log=q)
    assert rep["scale"] == "1:100" and abs(max(rep["size_mm"]) - 140) < 1 and abs(rep["longest_mm"] - 140) < 1
    assert abs(max(rep["real_m"]) - 14) < 0.1
    tilted = printprep.prepare(glb, tmp_path / "b", printprep.PrintOptions(scale=100, tilt_deg=20, mode="parts",
                               format="stl", exclude_nodes=EXCL), log=q)
    assert tilted["scale"] == "1:100"
    assert printprep.file_tag(printprep.PrintOptions(scale=12.5)) == "1-12_5"
    assert printprep.file_tag(printprep.PrintOptions(size_mm=150)) == "150mm"


def test_a_gun_beside_its_turret_is_joined_to_the_model():
    """A barrel held by a gimbal the game leaves out fuses as its own shell; it gets a
    strut to the body. Something far away is left alone."""
    from scipy import ndimage
    g = printprep.Grid([0, 0, 0], [40, 20, 20], 0.2, pad=2)
    g.v[20:120, 20:80, 20:80] = True        # turret / hull
    g.v[125:180, 45:55, 45:55] = True       # barrel 1 mm off its front
    g.v[20:30, 20:30, 102:110] = True       # a piece 4.4 mm away
    assert ndimage.label(g.v)[1] == 3
    assert printprep.connect_pieces(g, 1.2, 3.0, log=lambda *_: None) == 1
    labels, n = ndimage.label(g.v)
    assert n == 2 and labels[150, 50, 50] == labels[60, 50, 50]
    assert not g.v[190:, :, :].any()        # the strut doesn't poke out past the barrel's end
