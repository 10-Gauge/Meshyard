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


def test_stand_peg_stays_off_a_fin_and_can_be_moved():
    """The Stingray's bottom fin hangs under its center of mass: the post must not go
    through it, and the user can move the peg along the ship."""
    # posed nose-right: the ship's length runs along x, its right side toward -y
    g = printprep.Grid([0, 0, 0], [120, 60, 30], 0.5, pad=2)
    g.v[20:220, 10:110, 30:50] = True       # belly slab, 10 mm thick
    g.v[80:160, 58:62, 6:30] = True         # thin fin hanging 12 mm under the middle
    opts = printprep.PrintOptions(size_mm=120, stand="peg")
    s = printprep.plan_stand(g, opts, log=lambda *_: None)
    fin_y = g.origin[1] + 60 * g.pitch
    assert abs(s.y - fin_y) > s.post_r + 0.5          # the post clears the fin
    assert s.top_z > g.origin[2] + 25 * g.pitch       # it meets the belly, not the fin's edge
    moved = printprep.plan_stand(g, printprep.PrintOptions(size_mm=120, stand="peg", stand_forward_mm=-30),
                                 log=lambda *_: None)
    assert moved.x < s.x - 20                         # toward the tail
    right = printprep.plan_stand(g, printprep.PrintOptions(size_mm=120, stand="peg", stand_side_mm=15),
                                 log=lambda *_: None)
    assert right.y < s.y                              # toward the right wing (the stand's front)


def test_solid_glow_trim_prints_but_flat_glow_sheets_do_not(tmp_path):
    """The Alpha Wolf's nozzle rims are glow-named solid parts; light strips are flat."""
    sc = trimesh.Scene()
    sc.add_geometry(trimesh.creation.box((4, 1, 8)), node_name="hull", geom_name="hull")
    sc.add_geometry(trimesh.creation.annulus(0.3, 0.5, 0.4), node_name="glow_thruster_main", geom_name="rim",
                    transform=trimesh.transformations.translation_matrix([0, 0, 4.5]))
    sc.add_geometry(trimesh.creation.box((2, 0.004, 0.5)), node_name="glow_strip", geom_name="strip",
                    transform=trimesh.transformations.translation_matrix([0, 0.51, 0]))
    glb = tmp_path / "ship.glb"
    sc.export(glb)
    names = {n for n, _ in printprep.load_parts(glb, [])}
    assert "glow_thruster_main" in names and "glow_strip" not in names


def test_peg_preview_is_under_the_model_in_its_own_coordinates(tmp_path):
    # a 10 x 2 x 4 m slab (glTF: y up, so 2 m tall), centred 3 m off the origin in x
    slab = trimesh.creation.box(extents=[10, 2, 4])
    slab.apply_translation([3, 5, 0])
    path = tmp_path / "slab.glb"
    trimesh.Scene(slab).export(path)
    r = printprep.peg_preview(path, printprep.PrintOptions(stand="peg", size_mm=100))
    x, y, z = r["top"]
    assert abs(x - 3) < 0.3 and abs(z) < 0.3          # under the middle
    assert abs(y - 4) < 0.3                           # on the underside (5 - 2/2)
    assert np.allclose(r["down"], [0, -1, 0], atol=1e-6)
    assert 0 < r["peg_r"] < r["post_r"] < 1           # sizes in meters at 1:100
    moved = printprep.peg_preview(path, printprep.PrintOptions(stand="peg", size_mm=100, stand_side_mm=20))
    assert moved["top"][0] != x                       # the offset moves it
    assert printprep.peg_preview(path, printprep.PrintOptions(stand="none"))["stand"] == "none"


def _two_tone_parts():
    top = trimesh.creation.box(extents=[10, 4, 1]); top.apply_translation([0, 0, 0.5])
    bottom = trimesh.creation.box(extents=[10, 4, 1]); bottom.apply_translation([0, 0, -0.5])
    top.metadata["rgb"], bottom.metadata["rgb"] = (200, 30, 30), (30, 30, 200)
    return [("top", top), ("bottom", bottom)]


def test_painted_faces_take_the_nearest_surfaces_color():
    parts = _two_tone_parts()
    hull = trimesh.creation.box(extents=[10.2, 4.2, 2.2])     # the remeshed print around them
    hull = hull.subdivide().subdivide()
    faces, colors = printprep.paint_faces(hull, parts, 4, log=lambda *_: None)
    assert len(colors) == 2 and set(colors) == {"#C81E1E", "#1E1EC8"}
    up = hull.face_normals[:, 2] > 0.9
    down = hull.face_normals[:, 2] < -0.9
    red = colors.index("#C81E1E") + 1
    assert np.all(faces[up] == red) and np.all(faces[down] != red)


def test_bambu_3mf_carries_paint_and_filament_colors(tmp_path):
    import json as _json
    import zipfile
    box = trimesh.creation.box(extents=[1, 1, 1])
    out = printprep.write_bambu_3mf(tmp_path / "x.3mf", [("model", box)], {"model": np.array([1, 2] * 6)},
                                    ["#112233", "#445566"], "x")
    z = zipfile.ZipFile(out)
    model = z.read("3D/3dmodel.model").decode()
    # standard 3MF colors, which Bambu Studio matches to filaments when it opens the file
    assert '<m:color color="#112233FF"/>' in model and '<m:color color="#445566FF"/>' in model
    assert model.count('pid="2" p1="0"') == 6 and model.count('pid="2" p1="1"') == 6
    assert model.index("m:colorgroup") < model.index("<object")    # declared before use
    assert "paint_color" not in model           # Bambu would read slots, not colors
    assert _json.loads(z.read("Metadata/project_settings.config"))["filament_colour"] == ["#112233", "#445566"]
    assert 'lastid="11"' in z.read("Metadata/Slic3r_PE_model.config").decode()
    assert len(trimesh.load(out, force="mesh").faces) == 12   # still an ordinary 3MF


def test_loose_bits_are_dropped_but_real_parts_stay():
    g = printprep.Grid(np.zeros(3), np.array([40.0, 40.0, 40.0]), 1.0, 1)
    g.v[2:30, 2:30, 2:20] = True        # the model
    g.v[35:37, 35:37, 35:37] = True     # an 8-voxel speck far away
    g.v[2:12, 33:40, 2:12] = True       # a 700-voxel piece: big enough to keep
    assert printprep.drop_loose_bits(g, log=lambda *_: None) == 1
    assert not g.v[35:37, 35:37, 35:37].any() and g.v[5, 35, 5]


def test_tiny_shells_are_dropped_after_the_surface_fit():
    body = trimesh.creation.box((40, 20, 10))
    wing = trimesh.creation.box((5, 5, 5)).apply_translation((60, 0, 0))
    speck = trimesh.creation.box((0.5, 0.5, 0.5)).apply_translation((0, 30, 0))
    out = printprep.drop_small_shells(trimesh.util.concatenate([body, wing, speck]), log=lambda *_: None)
    vols = sorted(abs(s.volume) for s in out.split(only_watertight=False))
    assert vols == pytest.approx([125, 8000])


def test_closest_filament_names_a_bambu_color():
    assert printprep.closest_filament("#FEFEFE") == "Jade White"
    assert printprep.closest_filament("#8a8c8e") == "Gray"
    assert printprep.closest_filament("#c02a20") == "Red"


def test_ships_face_nose_right_and_incline_and_tilt_about_the_stand():
    """Nose (+y once Z-up, -z in the export) ends at +x, the right wing (+x in the
    export) toward the stand's front (-y); incline lifts the nose, tilt drops the right wing."""
    def posed(**kw):
        body = trimesh.creation.box((2, 1, 10))          # glTF: long along z, nose at -z
        nose = trimesh.creation.box((0.5, 0.5, 0.5)).apply_translation((0, 0, -6))
        wing = trimesh.creation.box((0.5, 0.5, 0.5)).apply_translation((3, 0, 0))   # right wing
        parts = [("body", body), ("nose", nose), ("wing", wing)]
        printprep._orient_and_scale(parts, 100, kw.get("tilt", 0), 0, kw.get("roll", 0))
        return {n: m.centroid for n, m in parts}
    p = posed()
    assert p["nose"][0] > p["body"][0] + 10 and abs(p["nose"][1] - p["body"][1]) < 1
    assert p["wing"][1] < p["body"][1] - 5
    up = posed(tilt=20)
    assert up["nose"][2] > up["body"][2] + 5
    bank = posed(roll=20)
    assert bank["wing"][2] < bank["body"][2] - 3


def _long_ship_grid():
    g = printprep.Grid([0, 0, 0], [200, 40, 30], 0.5, pad=2)
    g.v[20:380, 20:60, 30:50] = True        # a long belly slab, 200 mm x 20 mm
    return g


def test_two_posts_go_fore_and_aft_and_the_base_spans_them():
    g = _long_ship_grid()
    s = printprep.plan_stand(g, printprep.PrintOptions(size_mm=200, stand="peg", stand_posts=2,
                                                      stand_shape="rect"), log=lambda *_: None)
    posts = printprep.all_posts(s)
    assert len(posts) == 2
    xs = sorted(p[0] for p in posts)
    assert xs[1] - xs[0] > 60                              # well apart along the ship
    x0, x1, _, _ = printprep._base_box(s)
    assert x0 < xs[0] - s.post_r and x1 > xs[1] + s.post_r   # both stand on the base


def test_base_size_override_and_balance_check():
    g = _long_ship_grid()
    big = printprep.plan_stand(g, printprep.PrintOptions(size_mm=200, stand="peg", stand_shape="rect",
                                                        stand_base_w_mm=180, stand_base_d_mm=90), log=lambda *_: None)
    assert big.base_w == 180 and big.base_d == 90 and big.balance > 0
    small = printprep.plan_stand(g, printprep.PrintOptions(size_mm=200, stand="peg", stand_shape="round",
                                                          stand_base_w_mm=20, stand_forward_mm=80),
                                 log=lambda *_: None)
    assert small.balance < 0                              # balance point off a tiny base, far from the peg


def test_stand_faces_are_labeled_base_and_text_and_take_their_filaments():
    s = printprep.StandSpec(0.0, 0.0, 40.0, 4.0, 2.2, 6.0, 30.0)
    printprep._plan_rect(s, 150, 60, "STINGRAY", "Kruger")
    m = printprep.make_stand_mesh(s, 30.0, 0.3, peg=True, smooth=True, preview=True)
    lab = printprep.stand_face_labels(m.vertices, m.faces, s, 0.0, 30.0)
    assert (lab == 0).sum() > (lab == 2).sum() > 0 and (lab == 3).sum() > 0 and (lab >= 0).all()
    opts = printprep.PrintOptions(stand_base_color="#000000", stand_text_color="#E4BD68")
    colors, slots = printprep.stand_filaments(opts, ["#8A8C8E", "#000000"])
    assert colors == ["#8A8C8E", "#000000", "#E4BD68"]      # black reused, gold added
    assert slots == {0: 2, 1: 2, 2: 3, 3: 2}                # logo and second line follow the base


def test_colors_nobody_can_tell_apart_share_a_filament():
    import numpy as np
    from scsandbox import printprep
    centers = np.array([[64, 64, 64], [42, 42, 42], [191, 13, 13]], float)
    lab = np.array([0, 1, 2])
    weight = np.array([3.0, 1.0, 2.0])
    c, lab2, k = printprep._merge_close_colors(centers, lab, weight, 3)
    assert k == 2 and list(lab2) == [0, 0, 1]
    assert round(c[0][0]) == round((64 * 3 + 42) / 4)    # area-weighted mix
    assert list(c[1]) == [191, 13, 13]                   # red stays its own
