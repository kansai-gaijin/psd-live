# Reads a layered PSD/PSB without bpy, so it can run on a worker thread.
# Layer structure (names, groups, hidden flags, opacity) is parsed from the file itself, because
# OpenImageIO only reports the legacy ASCII name. Pixels are decoded by OpenImageIO (bundled with
# Blender): subimage k is layer record k-1 (bottom to top).
#
# The suffix after the last "_" of a layer name picks the map it belongs to: known suffixes merge into
# the standard maps (Skin_A, Freckles_A -> Albedo; Pores_N -> Normal), any other suffix makes a map named
# after the whole group or layer name, spaces turned into "_" (Blood_Mask and Dirt_Mask stay apart). A
# suffix on a group applies to the layers inside that have none of their own. Layers without a suffix
# are ignored. Every map is flattened bottom to top with normal blending and layer opacity; hidden
# layers are left out. A group's eye and opacity only change the Photoshop view (hide the Normal group
# to look at the albedo and it still exports). Each map is written as a canvas-sized PNG (EXR for
# 32-bit files) into <stem>_maps/ beside the PSD; unchanged maps are not rewritten.
import os, re, struct, zlib
import numpy as np
import OpenImageIO as oiio

LAYER, FOLDER_OPEN, FOLDER_CLOSED, GROUP_END = 0, 1, 2, 3
# Known suffix -> map name. Any other suffix makes a map named after the whole group/layer name.
MAPS = {"A": "Albedo", "ALBEDO": "Albedo", "BC": "Albedo", "BASECOLOR": "Albedo", "COLOR": "Albedo",
        "N": "Normal", "NORMAL": "Normal",
        "R": "Roughness", "ROUGHNESS": "Roughness",
        "M": "Metallic", "METALLIC": "Metallic",
        "S": "Specular", "SPECULAR": "Specular",
        "AO": "AO",
        "H": "Height", "HEIGHT": "Height",
        "E": "Emissive", "EMISSIVE": "Emissive", "EMISSION": "Emissive"}
ORDER = ["Albedo", "Normal", "Roughness", "Metallic", "Specular", "AO", "Height", "Emissive"]
STRAIGHT = {"Albedo", "Emissive"}            # colour + alpha; every other map is composited over black
SRGB = {"Albedo", "Emissive"}
SUFFIX = re.compile(r"_([A-Za-z0-9]+)\s*$")
SOCKET_BYTES = 50    # Blender names hold 63 bytes; leave room for " Alpha" and " (2)"
# Additional layer info keys whose length is 8 bytes in PSB files.
_PSB_LONG = {b"LMsk", b"Lr16", b"Lr32", b"Layr", b"Mt16", b"Mt32", b"Mtrn", b"Alph", b"FMsk", b"lnk2",
             b"FEid", b"FXid", b"PxSD"}


def _rd(f, fmt):
    fmt = ">" + fmt
    return struct.unpack(fmt, f.read(struct.calcsize(fmt)))


def file_sig(path):
    st = os.stat(path)
    return "%d:%d" % (st.st_mtime_ns, st.st_size)


def map_of(name):
    """Map name for a layer/group name, or None without a suffix."""
    m = SUFFIX.search(name)
    if not m:
        return None
    return MAPS.get(m.group(1).upper()) or re.sub(r"\s+", "_", name.strip())


def read_structure(path):
    """Canvas size, bit depth and the layer records bottom to top: name, kind, hidden, opacity."""
    with open(path, "rb") as f:
        sig, ver = _rd(f, "4sH")
        if sig != b"8BPS" or ver not in (1, 2):
            raise ValueError("not a PSD/PSB file")
        f.read(6)
        _nch, H, W, depth, mode = _rd(f, "HIIHH")
        f.seek(_rd(f, "I")[0], 1)                       # colour mode data
        f.seek(_rd(f, "I")[0], 1)                       # image resources
        big = "Q" if ver == 2 else "I"
        layers = []
        if _rd(f, big)[0] and _rd(f, big)[0]:           # layer and mask section, layer info
            count = abs(_rd(f, "h")[0])
            for _ in range(count):
                _t, _l, _b, _r, nch = _rd(f, "iiiiH")
                f.seek(nch * (2 + (8 if ver == 2 else 4)), 1)
                f.read(8)                               # '8BIM' + blend mode
                opacity, _clip, flags, _filler = _rd(f, "BBBB")
                extra = _rd(f, "I")[0]
                end = f.tell() + extra
                f.seek(_rd(f, "I")[0], 1)               # layer mask data
                f.seek(_rd(f, "I")[0], 1)               # blending ranges
                n = f.read(1)[0]
                name = f.read(n).decode("latin-1")
                f.seek((4 - (n + 1) % 4) % 4, 1)
                kind = LAYER
                while f.tell() + 12 <= end:
                    sig2, key = f.read(4), f.read(4)
                    if sig2 not in (b"8BIM", b"8B64"):
                        break
                    klen = _rd(f, "Q" if ver == 2 and key in _PSB_LONG else "I")[0]
                    data = f.read(klen)
                    if klen % 2:
                        f.read(1)
                    if key == b"luni" and len(data) >= 4:
                        cnt = struct.unpack(">I", data[:4])[0]
                        name = data[4:4 + cnt * 2].decode("utf-16-be", "replace").rstrip("\0") or name
                    elif key in (b"lsct", b"lsdk") and len(data) >= 4:
                        kind = struct.unpack(">I", data[:4])[0]
                f.seek(end)
                layers.append({"name": name, "kind": kind if kind in (1, 2, 3) else LAYER,
                               "hidden": bool(flags & 2), "opacity": opacity / 255.0})
    return {"W": W, "H": H, "depth": depth, "mode": mode, "layers": layers}


def plan(info):
    """{map name: [(subimage, opacity), ...] bottom to top} for the visible suffixed layers, plus the
    ordered list of every map that has at least one layer (hidden ones included, so pins stay put)."""
    maps, seen, stack = {}, [], []
    recs = info["layers"]
    for k in range(len(recs) - 1, -1, -1):              # top to bottom, to know the enclosing groups
        r = recs[k]
        if r["kind"] in (FOLDER_OPEN, FOLDER_CLOSED):
            stack.append(map_of(r["name"])); continue
        if r["kind"] == GROUP_END:
            if stack: stack.pop()
            continue
        m = map_of(r["name"]) or next((g for g in reversed(stack) if g), None)
        if m is None:
            continue
        if m not in seen:
            seen.append(m)
        if r["hidden"]:
            maps.setdefault(m, []); continue
        maps.setdefault(m, []).insert(0, (k + 1, r["opacity"]))   # insert at front -> bottom to top
    order = [m for m in ORDER if m in seen] + sorted((m for m in seen if m not in ORDER), key=str.lower)
    return order, maps


def socket_names(order):
    """Pin name per map, clipped so the ' Alpha' pin still fits Blender's 63-byte names."""
    out, used = {}, set()
    for m in order:
        base = m.encode("utf-8")[:SOCKET_BYTES].decode("utf-8", "ignore").rstrip() or "map"
        name, i = base, 2
        while name in used:
            name = "%s (%d)" % (base, i); i += 1
        used.add(name); out[m] = name
    return out


def _write(fn, arr, fmt):
    tmp = fn + ".tmp" + os.path.splitext(fn)[1]
    out = oiio.ImageOutput.create(tmp)
    if out is None:
        raise RuntimeError(oiio.geterror())
    spec = oiio.ImageSpec(arr.shape[1], arr.shape[0], arr.shape[2], fmt)
    spec.attribute("png:compressionLevel", 1)
    if not out.open(tmp, spec) or not out.write_image(arr):
        err = out.geterror(); out.close(); raise RuntimeError(err)
    out.close()
    os.replace(tmp, fn)


def _layer(inp, sub, W, H, read_t, full, np_t):
    """Layer subimage placed on the canvas as float32 RGBA 0-1 (None if empty)."""
    if not inp.seek_subimage(sub, 0):
        raise RuntimeError("layer count mismatch (file still being written?)")
    spec = inp.spec()
    if spec.width <= 0 or spec.height <= 0:
        return None
    px = inp.read_image(sub, 0, 0, spec.nchannels, read_t)
    if px is None:
        raise RuntimeError(inp.geterror() or "cannot read layer %d" % sub)
    px = px.reshape(spec.height, spec.width, spec.nchannels)
    x0, y0 = spec.x, spec.y
    sx0, sy0, dx0, dy0 = max(0, -x0), max(0, -y0), max(0, x0), max(0, y0)
    w, h = min(spec.width - sx0, W - dx0), min(spec.height - sy0, H - dy0)
    if w <= 0 or h <= 0:
        return None
    src = px[sy0:sy0 + h, sx0:sx0 + w].astype(np.float32) / full
    c = min(4, src.shape[2])
    out = np.zeros((H, W, 4), np.float32)
    out[dy0:dy0 + h, dx0:dx0 + w, :c] = src[..., :c]
    if c < 4:
        out[dy0:dy0 + h, dx0:dx0 + w, 3] = 1.0
    return out


def flatten(inp, name, layers, W, H, read_t, full, np_t):
    """One map: premultiplied 'over' bottom to top, then encoded for its kind."""
    rnd = 0.0 if np_t is np.float32 else 0.5
    if len(layers) == 1 and layers[0][1] >= 1.0:          # one opaque layer: the map is that layer as is
        L = _layer(inp, layers[0][0], W, H, read_t, full, np_t)
        if L is not None and L[..., 3].min() >= 1.0:
            return np.clip(L * full + rnd, 0, full).astype(np_t)
    rgb = np.zeros((H, W, 3), np.float32); a = np.zeros((H, W, 1), np.float32)
    if name == "Normal":
        rgb[...] = (0.5, 0.5, 1.0); a[...] = 1.0
    for sub, op in layers:
        L = _layer(inp, sub, W, H, read_t, full, np_t)
        if L is None or op <= 0:
            continue
        for y in range(0, H, 512):                       # chunked to keep temporaries small
            la = L[y:y + 512, :, 3:4] * op
            rgb[y:y + 512] = rgb[y:y + 512] * (1 - la) + L[y:y + 512, :, :3] * la
            a[y:y + 512] = a[y:y + 512] * (1 - la) + la
        del L
    out = np.empty((H, W, 4), np_t)
    for y in range(0, H, 512):
        c, al = rgb[y:y + 512], a[y:y + 512]
        if name == "Normal":                             # keep the result a unit normal
            v = c * 2 - 1
            v /= np.maximum(np.linalg.norm(v, axis=2, keepdims=True), 1e-6)
            c = v * 0.5 + 0.5
        elif name in STRAIGHT:
            c = np.where(al > 1e-6, c / np.maximum(al, 1e-6), 0)
        out[y:y + 512, :, :3] = np.clip(c * full + rnd, 0, full)
        out[y:y + 512, :, 3:] = np.clip(al * full + rnd, 0, full)
    return out


def export(path, cache_dir, prev_hashes):
    """Flattens and writes the maps that changed. Returns sig, size and one entry per map."""
    sig = file_sig(path)
    info = read_structure(path)
    W, H, depth = info["W"], info["H"], info["depth"]
    stem = os.path.splitext(os.path.basename(path))[0]
    order, maps = plan(info)
    socks = socket_names(order)
    if depth == 32:
        np_t, oi_t, ext, read_t, full = np.float32, oiio.HALF, ".exr", "float", 1.0
    elif depth == 16:
        np_t, oi_t, ext, read_t, full = np.uint16, oiio.UINT16, ".png", "uint16", 65535.0
    else:
        np_t, oi_t, ext, read_t, full = np.uint8, oiio.UINT8, ".png", "uint8", 255.0
    os.makedirs(cache_dir, exist_ok=True)
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise RuntimeError(oiio.geterror() or "cannot open PSD")
    entries, hashes, files = [], {}, set()
    try:
        for m in order:
            img = np.ascontiguousarray(flatten(inp, m, maps.get(m, []), W, H, read_t, full, np_t))
            hsh = zlib.adler32(img.data) ^ (W << 20) ^ H
            fn = os.path.join(cache_dir, "%s_%s%s" % (stem, re.sub(r'[<>:"/\\|?*]', "_", m), ext))
            changed = prev_hashes.get(m) != hsh or not os.path.exists(fn)
            if changed:
                _write(fn, img, oi_t)
            del img
            hashes[m] = hsh; files.add(os.path.normcase(fn))
            entries.append({"key": m, "name": "%s_%s" % (stem, m), "socket": socks[m], "file": fn, "changed": changed,
                            "srgb": m in SRGB, "layers": len(maps.get(m, []))})
    finally:
        inp.close()
    if file_sig(path) != sig:
        raise RuntimeError("file changed while reading")
    for f in os.listdir(cache_dir):                       # maps that no longer exist
        p = os.path.join(cache_dir, f)
        if os.path.splitext(f)[1].lower() in (".png", ".exr") and os.path.normcase(p) not in files and ".tmp" not in f:
            try:
                os.remove(p)
            except OSError:
                pass
    return {"sig": sig, "W": W, "H": H, "entries": entries, "hashes": hashes}
