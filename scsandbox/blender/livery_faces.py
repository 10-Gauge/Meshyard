"""Blender (background) script: pull the faces of given materials out of a StarBreaker
package mesh file (.blend), in each object's own coordinates, for the paints that switch
on parts the bundled export leaves out (the Idris Sharktooth's shark mouth decal).

    blender -b --python livery_faces.py -- out.json "Name1,Name2" mesh1.blend [mesh2.blend ...]

Output JSON: {"parts": [{"object", "material", "positions", "normals", "uv0", "uv1",
"triangles"}]} with the material's submaterial name (the part after "_mtl_", without
the trailing number).
"""

import json
import re
import sys

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
out_path, wanted, paths = args[0], set(args[1].split(",")), args[2:]
for path in paths:   # the hull and the parts that wear its material (the Idris's hangar doors)
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.objects = src.objects


def sub_name(mat_name: str) -> str:
    n = mat_name.split("_mtl_", 1)[-1]
    return re.sub(r"_\d+$", "", n)


parts = []
for ob in bpy.data.objects:
    if ob.type != "MESH":
        continue
    me = ob.data
    slots = {i: sub_name(m.name) for i, m in enumerate(me.materials) if m and sub_name(m.name) in wanted}
    if not slots:
        continue
    me.calc_loop_triangles()
    uvs = [me.uv_layers[i].data for i in range(min(2, len(me.uv_layers)))]
    for slot, sub in slots.items():
        pos, nrm, uv0, uv1, tris = [], [], [], [], []
        for t in me.loop_triangles:
            if t.material_index != slot:
                continue
            base = len(pos)
            for li in t.loops:
                lp = me.loops[li]
                v = me.vertices[lp.vertex_index]
                pos.append(list(v.co))
                nrm.append(list(v.normal))
                uv0.append(list(uvs[0][li].uv) if uvs else [0.0, 0.0])
                uv1.append(list(uvs[1][li].uv) if len(uvs) > 1 else [0.0, 0.0])
            tris.append([base, base + 1, base + 2])
        if tris:
            parts.append({"object": ob.name, "material": sub, "positions": pos, "normals": nrm,
                          "uv0": uv0, "uv1": uv1, "triangles": tris})
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"parts": parts}, f)
print("LIVERY FACES", len(parts))
