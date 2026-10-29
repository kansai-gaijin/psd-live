# PSD Live: a PSD's layers, grouped by name suffix, become one Blender image per map (Skin_A ->
# Albedo, Pores_N -> Normal; an unknown suffix like Blood_Mask makes a map named Blood_Mask), kept in
# sync while you paint in Photoshop. A "PSD Layers" shader node has a Color and an Alpha output per map.
#
# How it works
# - A background thread reads the PSD (psd_file.py), flattens the layers of each map and writes the maps
#   as canvas-sized PNGs into <stem>_maps/ beside it. The images are ordinary file images, so they
#   save with the .blend. Layers without a suffix are ignored.
#   A group's eye only changes the Photoshop view; a layer's eye decides whether it counts.
# - A timer polls the PSD's modified time; once a save has settled it re-exports, and only maps whose
#   pixels changed are reloaded.
# - The node is a custom group node: its private node tree holds one Image Texture per map, so it
#   renders in EEVEE and Cycles like any group. Each PSD consumer (node or a standalone link made from
#   the Image Editor) is a node tree tagged with the custom property "psd_live_path".
import os, re, sys, time, queue, threading, traceback, subprocess
import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty, BoolProperty, EnumProperty
from . import psd_file

POLL = 0.5           # seconds between file checks
SETTLE = 0.6         # wait this long after the last change before reading (Photoshop writes in chunks)

_state = {}          # normalised PSD path -> watcher state
_done = queue.Queue()


# ----------------------------------------------------------------------------------------------- paths

def _real(p):
    return os.path.abspath(bpy.path.abspath(p))


def _key(p):
    return os.path.normcase(_real(p))


def _stem(p):
    return os.path.splitext(os.path.basename(p))[0] if p else ""


def _rel(p):
    try:
        return bpy.path.relpath(p) if bpy.data.filepath else p
    except ValueError:                  # other drive
        return p


def consumers():
    """{key: (real path, [trees])} for every node tree linked to a PSD."""
    out = {}
    for t in bpy.data.node_groups:
        p = t.get("psd_live_path")
        if p:
            k = _key(p)
            out.setdefault(k, (_real(p), []))[1].append(t)
    return out


def layer_images(key):
    return [i for i in bpy.data.images if i.get("psd_live_src") == key]


# --------------------------------------------------------------------------------------------- watcher

def _st(key, trees):
    s = _state.get(key)
    if s is None:
        imgs = layer_images(key)
        files_ok = bool(imgs) and all(os.path.exists(_real(i.filepath)) for i in imgs)
        t = trees[0]
        s = _state[key] = {
            "sig": t.get("psd_live_sig") if files_ok else None,
            "hashes": {k: int(v) for k, v in t.get("psd_live_hashes", {}).items()} if files_ok else {},
            "due": 0.0, "force": False, "busy": False, "error": "", "tries": 0,
            "last": 0.0, "count": len(imgs)}
    return s


def request(path, force=True):
    """Schedule an export now (force also rewrites every layer)."""
    k = _key(path)
    s = _state.get(k)
    if s is None:
        c = consumers().get(k)
        if not c:
            return
        s = _st(k, c[1])
    if force:
        s["hashes"] = {}
    s["force"] = True


def _run(real, hashes):
    cache = os.path.join(os.path.dirname(real), _stem(real) + "_maps")
    try:
        return psd_file.export(real, cache, hashes)
    except Exception as e:
        return e


def _start(key, real, s):
    s["busy"], s["force"], s["due"] = True, False, 0.0
    hashes = dict(s["hashes"])
    threading.Thread(target=lambda: _done.put((key, real, _run(real, hashes))), daemon=True).start()


def _finish(key, real, res):
    s = _state.get(key)
    if s is None:
        return
    s["busy"] = False
    if isinstance(res, Exception):
        s["error"] = str(res)
        s["tries"] += 1
        if s["tries"] <= 8:                       # mid-save read, locked file...: try again shortly
            s["due"] = time.monotonic() + 1.0
        return
    s.update(error="", tries=0, hashes=res["hashes"], sig=res["sig"], last=time.time(),
             count=len(res["entries"]))
    c = consumers().get(key)
    if c:
        apply_result(key, res, c[1])
    _redraw()


def _tick():
    try:
        while True:
            try:
                key, real, res = _done.get_nowait()
            except queue.Empty:
                break
            _finish(key, real, res)
        now = time.monotonic()
        for key, (real, trees) in consumers().items():
            s = _st(key, trees)
            if any(t.get("psd_live_watch", True) for t in trees) or s["force"]:
                try:
                    sig = psd_file.file_sig(real)
                except OSError:
                    s["error"] = "file not found"
                    continue
                if s["error"] == "file not found":
                    s["error"] = ""
                if sig != s["sig"]:
                    s["sig"], s["due"] = sig, now + SETTLE
            if not s["busy"] and (s["force"] or (s["due"] and now >= s["due"])):
                _start(key, real, s)
    except Exception:
        traceback.print_exc()
    return POLL


def _redraw():
    wm = bpy.context.window_manager
    for win in (wm.windows if wm else []):
        for area in win.screen.areas:
            area.tag_redraw()


# -------------------------------------------------------------------------------------- images + trees

def apply_result(key, res, trees):
    imgs = {}
    existing = {i.get("psd_live_key"): i for i in layer_images(key)}
    for e in res["entries"]:
        img = existing.get(e["key"])
        if img is None:
            img = bpy.data.images.load(e["file"], check_existing=False)
            img.name = e["name"]
            img["psd_live_src"], img["psd_live_key"] = key, e["key"]
            if not e["srgb"]:
                img.colorspace_settings.name = "Non-Color"
            img.filepath = _rel(e["file"])
        elif os.path.normcase(_real(img.filepath)) != os.path.normcase(e["file"]):
            img.filepath = _rel(e["file"])
        elif e["changed"]:
            img.reload()
        imgs[e["key"]] = img
    hashes = {k: str(v) for k, v in res["hashes"].items()}
    for t in trees:
        t["psd_live_sig"], t["psd_live_hashes"] = res["sig"], hashes
        if t.get("psd_live_node", False):
            sync_tree(t, res["entries"], imgs)
    for img in layer_images(key):                 # maps that are gone (or images from older versions)
        if img.get("psd_live_key") not in imgs and img.users == 0:
            bpy.data.images.remove(img)


def _outputs(tree):
    return [i for i in tree.interface.items_tree if i.item_type == "SOCKET" and i.in_out == "OUTPUT"]


def new_tree(path=""):
    t = bpy.data.node_groups.new(".PSD " + (_stem(path) or "Layers"), "ShaderNodeTree")
    s = t.interface.new_socket("Vector", in_out="INPUT", socket_type="NodeSocketVector")
    s.hide_value = True
    t.nodes.new("NodeGroupOutput")
    t["psd_live_watch"] = True
    if path:
        t["psd_live_path"] = path
    return t


def sync_tree(tree, entries, imgs):
    """Output sockets = a Color + Alpha pair per map (kept sockets keep their links)."""
    want = []
    for e in entries:
        want += [(e["socket"], "NodeSocketColor"), (e["socket"] + " Alpha", "NodeSocketFloat")]
    types = dict(want)
    iface = tree.interface
    for item in _outputs(tree):
        if types.get(item.name) != item.socket_type:
            iface.remove(item)
    have = {i.name: i for i in _outputs(tree)}
    for name, typ in want:
        if name not in have:
            have[name] = iface.new_socket(name, in_out="OUTPUT", socket_type=typ)
    for pos, (name, _t) in enumerate(want):
        if _outputs(tree).index(have[name]) != pos:
            iface.move(have[name], pos)
    sig = repr([(e["key"], e["socket"]) for e in entries])
    if tree.get("psd_live_struct") == sig and len(tree.nodes) > 2:
        for n in tree.nodes:                     # same layout: just make sure images are assigned
            if n.bl_idname == "ShaderNodeTexImage" and n.get("psd_live_key") in imgs:
                n.image = imgs[n["psd_live_key"]]
        return
    build_nodes(tree, entries, imgs)
    tree["psd_live_struct"] = sig


def build_nodes(tree, entries, imgs):
    nodes, links = tree.nodes, tree.links
    nodes.clear()
    gi = nodes.new("NodeGroupInput"); gi.location = (-700, 0)
    tc = nodes.new("ShaderNodeTexCoord"); tc.location = (-700, -200)
    go = nodes.new("NodeGroupOutput"); go.location = (400, 0)
    outs = {s.name: s for s in go.inputs if s.bl_idname != "NodeSocketVirtual"}
    interp = tree.get("psd_live_interp", "Linear")
    for i, e in enumerate(entries):
        tex = nodes.new("ShaderNodeTexImage")
        tex.location = (-300, -i * 280)
        tex.label = e["socket"]
        tex.image = imgs[e["key"]]
        tex.interpolation = interp
        tex.extension = "REPEAT"
        tex["psd_live_key"] = e["key"]
        links.new(tex.outputs["Color"], outs[e["socket"]])
        links.new(tex.outputs["Alpha"], outs[e["socket"] + " Alpha"])
    relink_vectors(tree)


def relink_vectors(tree):
    """Image vectors come from the node's Vector input when it is linked, else from the UVs."""
    gi = next((n for n in tree.nodes if n.bl_idname == "NodeGroupInput"), None)
    tc = next((n for n in tree.nodes if n.bl_idname == "ShaderNodeTexCoord"), None)
    if not gi or not tc:
        return
    src = gi.outputs[0] if tree.get("psd_live_vec", False) else tc.outputs["UV"]
    for n in tree.nodes:
        if n.bl_idname == "ShaderNodeTexImage":
            tree.links.new(src, n.inputs["Vector"])


# ------------------------------------------------------------------------------------------------ node

def _node_path_changed(self, context):
    t = self.node_tree
    if t is None:
        return
    t["psd_live_path"] = self.filepath
    t.name = ".PSD " + (_stem(self.filepath) or "Layers")
    for k in ("psd_live_sig", "psd_live_hashes", "psd_live_struct"):
        t.pop(k, None)
    if self.filepath:
        _state.pop(_key(self.filepath), None)
        request(self.filepath)


def _node_live_changed(self, context):
    if self.node_tree:
        self.node_tree["psd_live_watch"] = self.live


def _node_interp_changed(self, context):
    t = self.node_tree
    if t:
        t["psd_live_interp"] = self.interpolation
        for n in t.nodes:
            if n.bl_idname == "ShaderNodeTexImage":
                n.interpolation = self.interpolation


class ShaderNodePSDLive(bpy.types.ShaderNodeCustomGroup):
    """PSD layers flattened per name suffix (_A albedo, _N normal, _R roughness...), live-linked: a Color and an Alpha output per map"""
    bl_idname = "ShaderNodePSDLive"
    bl_label = "PSD Layers"
    bl_width_default = 260

    filepath: StringProperty(name="PSD", subtype="FILE_PATH", update=_node_path_changed,
                           options={"PATH_SUPPORTS_BLEND_RELATIVE"})
    live: BoolProperty(name="Live", default=True, update=_node_live_changed,
                       description="Reload the layers whenever the PSD is saved")
    interpolation: EnumProperty(name="Interpolation", update=_node_interp_changed, items=[
        ("Linear", "Linear", ""), ("Closest", "Closest", ""), ("Cubic", "Cubic", ""), ("Smart", "Smart", "")])

    def init(self, context):
        self.node_tree = new_tree()
        self.node_tree["psd_live_node"] = True

    def copy(self, node):
        if node.node_tree:
            self.node_tree = node.node_tree.copy()

    def free(self):
        t = self.node_tree
        if t and t.users <= 1:
            try:
                bpy.data.node_groups.remove(t)
            except Exception:
                pass

    def update(self):
        t = self.node_tree
        if t is None or not self.inputs:
            return
        linked = self.inputs[0].is_linked
        if bool(t.get("psd_live_vec", False)) != linked:
            t["psd_live_vec"] = linked
            relink_vectors(t)

    def draw_label(self):
        return _stem(self.filepath) or "PSD Layers"

    def draw_buttons(self, context, layout):
        layout.prop(self, "filepath", text="")
        if not self.filepath:
            return
        row = layout.row(align=True)
        row.prop(self, "live", toggle=True, icon="REC" if self.live else "PAUSE")
        row.operator("psd_live.reload", text="", icon="FILE_REFRESH").path = self.filepath
        row.operator("psd_live.edit", text="", icon="GREASEPENCIL").path = self.filepath
        layout.label(text=status_text(_key(self.filepath)))

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)
        layout.prop(self, "interpolation")


def status_text(key):
    s = _state.get(key)
    if s is None:
        return "waiting..."
    if s["busy"]:
        return "updating..."
    if s["error"]:
        return "error: " + s["error"][:60]
    if s["last"]:
        return "%d maps, updated %s" % (s["count"], time.strftime("%H:%M:%S", time.localtime(s["last"])))
    return "%d maps" % s["count"]


# ------------------------------------------------------------------------------------------- operators

class PSDLIVE_OT_open(bpy.types.Operator):
    """Link a PSD: layers named with a map suffix (_A, _N, _R, _M, _Mask...) become one image per map that follows the file while you paint.
In a shader editor this also adds a PSD Layers node"""
    bl_idname = "psd_live.open"
    bl_label = "Open PSD Layers (Live)"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.psd;*.psb", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "No PSD selected")
            return {"CANCELLED"}
        path = _rel(self.filepath)
        sp = context.space_data
        if sp and sp.type == "NODE_EDITOR" and sp.tree_type == "ShaderNodeTree" and sp.edit_tree:
            tree = sp.edit_tree
            for n in tree.nodes:
                n.select = False
            node = tree.nodes.new("ShaderNodePSDLive")
            node.location = sp.cursor_location
            node.filepath = path
            tree.nodes.active = node
        else:
            if _key(path) in consumers():
                request(path)
                return {"FINISHED"}
            t = new_tree(path)
            t.use_fake_user = True
            request(path)
        return {"FINISHED"}


class PSDLIVE_OT_reload(bpy.types.Operator):
    """Re-read every layer of this PSD now"""
    bl_idname = "psd_live.reload"
    bl_label = "Reload PSD"
    path: StringProperty()

    def execute(self, context):
        request(self.path)
        return {"FINISHED"}


class PSDLIVE_OT_edit(bpy.types.Operator):
    """Open the PSD in its default app (Photoshop)"""
    bl_idname = "psd_live.edit"
    bl_label = "Edit PSD"
    path: StringProperty()

    def execute(self, context):
        p = _real(self.path)
        if not os.path.isfile(p):
            self.report({"ERROR"}, "File not found: " + p)
            return {"CANCELLED"}
        if sys.platform == "win32":
            os.startfile(p)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
        return {"FINISHED"}


class PSDLIVE_OT_unlink(bpy.types.Operator):
    """Stop following this PSD (its layer images stay)"""
    bl_idname = "psd_live.unlink"
    bl_label = "Unlink PSD"
    bl_options = {"REGISTER", "UNDO"}
    path: StringProperty()

    def execute(self, context):
        c = consumers().get(_key(self.path))
        for t in (c[1] if c else []):
            if not t.get("psd_live_node", False):
                bpy.data.node_groups.remove(t)
        return {"FINISHED"}


# ----------------------------------------------------------------------------------------------- panels

class _PSDPanel:
    bl_region_type = "UI"
    bl_category = "PSD"
    bl_label = "PSD Live"

    def draw(self, context):
        layout = self.layout
        layout.operator("psd_live.open", icon="FILE_IMAGE")
        for key, (real, trees) in sorted(consumers().items()):
            box = layout.box()
            row = box.row(align=True)
            row.label(text=os.path.basename(real), icon="IMAGE_DATA")
            t0 = trees[0]
            path = t0["psd_live_path"]
            if not any(t.get("psd_live_node", False) for t in trees):
                row.prop(t0, '["psd_live_watch"]', text="", icon="REC")
                row.operator("psd_live.unlink", text="", icon="X").path = path
            row.operator("psd_live.reload", text="", icon="FILE_REFRESH").path = path
            row.operator("psd_live.edit", text="", icon="GREASEPENCIL").path = path
            box.label(text=status_text(key))


class PSDLIVE_PT_node(_PSDPanel, bpy.types.Panel):
    bl_idname = "PSDLIVE_PT_node"
    bl_space_type = "NODE_EDITOR"

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == "ShaderNodeTree"


class PSDLIVE_PT_image(_PSDPanel, bpy.types.Panel):
    bl_idname = "PSDLIVE_PT_image"
    bl_space_type = "IMAGE_EDITOR"


def _menu_node(self, context):
    if context.space_data.tree_type == "ShaderNodeTree":
        self.layout.separator()
        self.layout.operator("psd_live.open", text="PSD Layers (Live)", icon="FILE_IMAGE")


def _menu_image(self, context):
    self.layout.separator()
    self.layout.operator("psd_live.open", text="Open PSD Layers (Live)", icon="FILE_IMAGE")


@persistent
def _on_load(_dummy):
    _state.clear()


classes = (ShaderNodePSDLive, PSDLIVE_OT_open, PSDLIVE_OT_reload, PSDLIVE_OT_edit, PSDLIVE_OT_unlink,
           PSDLIVE_PT_node, PSDLIVE_PT_image)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.NODE_MT_add.append(_menu_node)
    bpy.types.IMAGE_MT_image.append(_menu_image)
    bpy.app.handlers.load_post.append(_on_load)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=POLL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    bpy.types.IMAGE_MT_image.remove(_menu_image)
    bpy.types.NODE_MT_add.remove(_menu_node)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
