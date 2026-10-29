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
# layers are left out. Layer blend modes are applied (Multiply, Screen, Overlay... see BLEND_MODES;
# Dissolve and group blend modes act like Normal / Pass Through). Layer masks count (their own position, background colour, density and the
# disabled flag), and a group's mask applies to every layer inside it; vector masks only when Photoshop
# stored them rendered. A group's eye and opacity only change the Photoshop view (hide the Normal group
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
# Photoshop blend mode keys that change the result; anything else (norm, pass, diss) is Normal.
BLEND_MODES = {"mul ": "Multiply", "scrn": "Screen", "over": "Overlay", "sLit": "Soft Light", "hLit": "Hard Light",
               "dark": "Darken", "lite": "Lighten", "idiv": "Color Burn", "div ": "Color Dodge",
               "lbrn": "Linear Burn", "lddg": "Linear Dodge", "diff": "Difference", "smud": "Exclusion",
               "fsub": "Subtract", "fdiv": "Divide", "vLit": "Vivid Light", "lLit": "Linear Light",
               "pLit": "Pin Light", "hMix": "Hard Mix", "dkCl": "Darker Color", "lgCl": "Lighter Color",
               "hue ": "Hue", "sat ": "Saturation", "colr": "Color", "lum ": "Luminosity"}
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
    """Canvas size, bit depth and the layer records bottom to top: name, kind, hidden, opacity, rect,
    channels (id, length), file offset of their pixel data and the layer mask settings."""
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
                t, l, b, r, nch = _rd(f, "iiiiH")
                chans = [(_rd(f, "h")[0], _rd(f, big)[0]) for _c in range(nch)]
                f.read(4)                               # '8BIM'
                blend = f.read(4).decode("latin-1")
                opacity, _clip, flags, _filler = _rd(f, "BBBB")
                extra = _rd(f, "I")[0]
                end = f.tell() + extra
                mask = _read_mask_data(f)
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
                               "hidden": bool(flags & 2), "opacity": opacity / 255.0,
                               "rect": (t, l, b, r), "channels": chans, "mask": mask, "blend": blend})
            off = f.tell()                              # channel image data follows the records
            for L in layers:
                L["chan_off"] = off
                off += sum(n for _i, n in L["channels"])
    return {"W": W, "H": H, "depth": depth, "mode": mode, "version": ver, "layers": layers}


def _read_mask_data(f):
    """The layer mask data block: rect, background colour, flags, densities and, when a layer has both
    a pixel mask and a vector mask, the 'real' rect/background/flags of the pixel mask (channel -3)."""
    size = _rd(f, "I")[0]
    end = f.tell() + size
    mask = None
    if size >= 18:
        rect = _rd(f, "iiii")
        default, flags = _rd(f, "BB")
        udens = vdens = 1.0
        real = None
        if size > 18 and flags & 16:                    # mask parameters (else size 20 = padding)
            pf = _rd(f, "B")[0]
            if pf & 1: udens = _rd(f, "B")[0] / 255.0
            if pf & 2: f.read(8)                        # user mask feather (not applied)
            if pf & 4: vdens = _rd(f, "B")[0] / 255.0
            if pf & 8: f.read(8)                        # vector mask feather
        if size > 20:
            if f.tell() + 18 <= end:
                rflags, rdefault = _rd(f, "BB")
                real = (_rd(f, "iiii"), rdefault, rflags)
        mask = {"rect": rect, "default": default, "flags": flags, "udens": udens, "vdens": vdens, "real": real}
    f.seek(end)
    return mask


def mask_specs(rec):
    """Masks to multiply into a record's alpha: (file offset, length, rect, background 0-255, density)."""
    m = rec.get("mask")
    if not m:
        return []
    offs, o = {}, rec["chan_off"]
    for cid, n in rec["channels"]:
        offs[cid] = (o, n); o += n

    def placed(rect, flags):
        if flags & 1:                                   # position relative to the layer
            t, l = rec["rect"][:2]
            return (rect[0] + t, rect[1] + l, rect[2] + t, rect[3] + l)
        return rect
    out = []
    if -3 in offs and m["real"]:                        # pixel mask + vector mask
        rect, default, rflags = m["real"]
        if not rflags & 2:
            out.append(offs[-3] + (placed(rect, rflags), default, m["udens"]))
        if -2 in offs and not m["flags"] & 2:
            out.append(offs[-2] + (placed(m["rect"], m["flags"]), m["default"], m["vdens"]))
    elif -2 in offs and not m["flags"] & 2:
        dens = m["vdens"] if m["flags"] & 8 else m["udens"]   # bit 3: rendered from a vector mask
        out.append(offs[-2] + (placed(m["rect"], m["flags"]), m["default"], dens))
    return out


def plan(info):
    """{map name: [(subimage, opacity, masks, blend key), ...] bottom to top} for the visible suffixed
    layers (masks = the layer's own plus those of its groups), and the ordered list of every map that
    has at least one layer (hidden ones included, so pins stay put)."""
    maps, seen, stack = {}, [], []
    recs = info["layers"]
    for k in range(len(recs) - 1, -1, -1):              # top to bottom, to know the enclosing groups
        r = recs[k]
        if r["kind"] in (FOLDER_OPEN, FOLDER_CLOSED):
            stack.append((map_of(r["name"]), mask_specs(r))); continue
        if r["kind"] == GROUP_END:
            if stack: stack.pop()
            continue
        m = map_of(r["name"]) or next((g[0] for g in reversed(stack) if g[0]), None)
        if m is None:
            continue
        if m not in seen:
            seen.append(m)
        if r["hidden"]:
            maps.setdefault(m, []); continue
        masks = mask_specs(r) + [spec for g in stack for spec in g[1]]
        maps.setdefault(m, []).insert(0, (k + 1, r["opacity"], masks, r["blend"]))   # front -> bottom to top
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


def _unpackbits(data, n):
    out = bytearray(); i = 0; size = len(data)
    while i < size and len(out) < n:
        h = data[i]; i += 1
        if h < 128:
            out += data[i:i + h + 1]; i += h + 1
        elif h > 128:
            out += data[i:i + 1] * (257 - h); i += 1
    return bytes(out[:n]).ljust(n, b"\0")


def read_mask(ctx, spec):
    """One mask channel as a canvas-sized float32 0-1 (background colour outside its rect)."""
    off, length, (t, l, b, r), default, dens = spec
    W, H, depth = ctx["W"], ctx["H"], ctx["depth"]
    canvas = np.full((H, W), default / 255.0, np.float32)
    w, h = r - l, b - t
    if w > 0 and h > 0 and length > 2:
        f = ctx["fh"]; f.seek(off)
        comp = _rd(f, "H")[0]; data = f.read(length - 2)
        bpp = max(1, depth // 8); n = w * h * bpp
        if comp == 0:
            raw = data[:n]
        elif comp == 1:
            raw = _unpackbits(data[h * (4 if ctx["version"] == 2 else 2):], n)
        elif comp in (2, 3):
            raw = zlib.decompress(data)
        else:
            raise RuntimeError("unknown mask compression %d" % comp)
        raw = raw.ljust(n, b"\0")[:n]
        if depth == 32:
            if comp == 3:                               # byte planes per row, delta coded
                a = np.cumsum(np.frombuffer(raw, np.uint8).reshape(h, w * 4), axis=1, dtype=np.uint8)
                raw = a.reshape(h, 4, w).transpose(0, 2, 1).tobytes()
            px = np.frombuffer(raw, ">f4").reshape(h, w).astype(np.float32)
        elif depth == 16:
            px = np.frombuffer(raw, ">u2").reshape(h, w)
            if comp == 3: px = np.cumsum(px, axis=1, dtype=np.uint16)
            px = px.astype(np.float32) / 65535.0
        else:
            px = np.frombuffer(raw, np.uint8).reshape(h, w)
            if comp == 3: px = np.cumsum(px, axis=1, dtype=np.uint8)
            px = px.astype(np.float32) / 255.0
        sx0, sy0, dx0, dy0 = max(0, -l), max(0, -t), max(0, l), max(0, t)
        cw, ch = min(w - sx0, W - dx0), min(h - sy0, H - dy0)
        if cw > 0 and ch > 0:
            canvas[dy0:dy0 + ch, dx0:dx0 + cw] = px[sy0:sy0 + ch, sx0:sx0 + cw]
    if dens < 1.0:
        canvas = 1.0 - dens * (1.0 - canvas)
    return canvas


def _layer(ctx, sub, masks=()):
    """Layer subimage placed on the canvas as float32 RGBA 0-1, masks applied (None if empty)."""
    inp, W, H, full = ctx["inp"], ctx["W"], ctx["H"], ctx["full"]
    if not inp.seek_subimage(sub, 0):
        raise RuntimeError("layer count mismatch (file still being written?)")
    spec = inp.spec()
    if spec.width <= 0 or spec.height <= 0:
        return None
    px = inp.read_image(sub, 0, 0, spec.nchannels, ctx["read_t"])
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
    for spec_m in masks:
        out[..., 3] *= read_mask(ctx, spec_m)
    return out


def _lum(c):
    return c[..., 0:1] * 0.3 + c[..., 1:2] * 0.59 + c[..., 2:3] * 0.11


def _clip_color(c):
    l = _lum(c); n = c.min(-1, keepdims=True); x = c.max(-1, keepdims=True)
    c = np.where(n < 0, l + (c - l) * l / np.maximum(l - n, 1e-6), c)
    return np.where(x > 1, l + (c - l) * (1 - l) / np.maximum(x - l, 1e-6), c)


def _set_lum(c, l):
    return _clip_color(c + (l - _lum(c)))


def _set_sat(c, sat):
    n = c.min(-1, keepdims=True); x = c.max(-1, keepdims=True); rng = x - n
    return np.where(rng > 1e-6, (c - n) * sat / np.maximum(rng, 1e-6), 0.0)


def _sat(c):
    return c.max(-1, keepdims=True) - c.min(-1, keepdims=True)


def _safe_div(a, b):
    return a / np.where(b == 0, 1.0, b)


def blend(key, cb, cs):
    """Photoshop blend mode B(backdrop, source) on straight colours 0-1 (W3C compositing formulas,
    Photoshop's own Soft Light and Hard Mix)."""
    if key == "mul ": return cb * cs
    if key == "scrn": return cb + cs - cb * cs
    if key == "hLit": return np.where(cs <= 0.5, 2 * cb * cs, 1 - 2 * (1 - cb) * (1 - cs))
    if key == "over": return np.where(cb <= 0.5, 2 * cb * cs, 1 - 2 * (1 - cb) * (1 - cs))
    if key == "sLit": return np.where(cs <= 0.5, cb - (1 - 2 * cs) * cb * (1 - cb), cb + (2 * cs - 1) * (np.sqrt(cb) - cb))
    if key == "dark": return np.minimum(cb, cs)
    if key == "lite": return np.maximum(cb, cs)
    if key == "div ":
        return np.where(cb <= 0, 0.0, np.where(cs >= 1, 1.0, np.minimum(1.0, _safe_div(cb, 1 - cs))))
    if key == "idiv":
        return np.where(cb >= 1, 1.0, np.where(cs <= 0, 0.0, 1 - np.minimum(1.0, _safe_div(1 - cb, cs))))
    if key == "lbrn": return np.maximum(0.0, cb + cs - 1)
    if key == "lddg": return np.minimum(1.0, cb + cs)
    if key == "diff": return np.abs(cb - cs)
    if key == "smud": return cb + cs - 2 * cb * cs
    if key == "fsub": return np.maximum(0.0, cb - cs)
    if key == "fdiv": return np.where(cs <= 0, np.where(cb > 0, 1.0, 0.0), np.minimum(1.0, _safe_div(cb, cs)))
    if key == "vLit": return np.where(cs <= 0.5, blend("idiv", cb, 2 * cs), blend("div ", cb, 2 * cs - 1))
    if key == "lLit": return np.clip(cb + 2 * cs - 1, 0.0, 1.0)
    if key == "pLit": return np.where(cs <= 0.5, np.minimum(cb, 2 * cs), np.maximum(cb, 2 * cs - 1))
    if key == "hMix": return np.where(cb + cs >= 1.0 - 1e-6, 1.0, 0.0)
    if key == "dkCl": return np.where(_lum(cs) < _lum(cb), cs, cb)
    if key == "lgCl": return np.where(_lum(cs) > _lum(cb), cs, cb)
    if key == "hue ": return _set_lum(_set_sat(cs, _sat(cb)), _lum(cb))
    if key == "sat ": return _set_lum(_set_sat(cb, _sat(cs)), _lum(cb))
    if key == "colr": return _set_lum(cs, _lum(cb))
    if key == "lum ": return _set_lum(cb, _lum(cs))
    return cs


def flatten(ctx, name, layers):
    """One map: premultiplied 'over' bottom to top, then encoded for its kind."""
    W, H, full, np_t = ctx["W"], ctx["H"], ctx["full"], ctx["np_t"]
    rnd = 0.0 if np_t is np.float32 else 0.5
    if len(layers) == 1 and layers[0][1] >= 1.0 and (name != "Normal" or layers[0][3] not in BLEND_MODES):
        L = _layer(ctx, layers[0][0], layers[0][2])         # one opaque layer on nothing: the map is that layer
        if L is not None and L[..., 3].min() >= 1.0:
            return np.clip(L * full + rnd, 0, full).astype(np_t)
    rgb = np.zeros((H, W, 3), np.float32); a = np.zeros((H, W, 1), np.float32)
    if name == "Normal":
        rgb[...] = (0.5, 0.5, 1.0); a[...] = 1.0
    for sub, op, masks, key in layers:
        L = _layer(ctx, sub, masks) if op > 0 else None
        if L is None:
            continue
        for y in range(0, H, 512):                       # chunked to keep temporaries small
            la = L[y:y + 512, :, 3:4] * op
            cs = L[y:y + 512, :, :3]
            if key in BLEND_MODES:                       # W3C: Cs' = (1 - ab) Cs + ab B(Cb, Cs), then 'over'
                ab = a[y:y + 512]
                cb = np.clip(np.where(ab > 1e-6, rgb[y:y + 512] / np.maximum(ab, 1e-6), 0.0), 0.0, 1.0)
                cs = (1 - ab) * cs + ab * np.clip(blend(key, cb, cs), 0.0, 1.0)
            rgb[y:y + 512] = rgb[y:y + 512] * (1 - la) + cs * la
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
    config = oiio.ImageSpec()
    config.attribute("oiio:UnassociatedAlpha", 1)         # keep Photoshop's straight colour; OIIO premultiplies by
    inp = oiio.ImageInput.open(path, config)               # default, which darkened every soft edge a second time
    if inp is None:
        raise RuntimeError(oiio.geterror() or "cannot open PSD")
    fh = open(path, "rb")                                 # for the mask channels
    ctx = {"inp": inp, "fh": fh, "W": W, "H": H, "depth": depth, "version": info["version"],
           "read_t": read_t, "full": full, "np_t": np_t}
    entries, hashes, files = [], {}, set()
    try:
        for m in order:
            img = np.ascontiguousarray(flatten(ctx, m, maps.get(m, [])))
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
        inp.close(); fh.close()
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
