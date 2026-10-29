# PSD Live

Paint your textures in Photoshop and see them in Blender's viewport a few seconds after each save.

PSD Live is a Blender add-on that keeps a live link to a layered `.psd`. You name your layers with a short suffix (`_A` for albedo, `_N` for normal, `_R` for roughness...). PSD Live flattens every map from its layers and hands you one shader node with a pin per map. Save in Photoshop and the maps that changed reload in Blender automatically.

It's meant for authoring: paint in Photoshop, preview in Blender, then bake the final textures for your engine.

- **One shader node, one pin per map.** It renders in EEVEE and Cycles like any node group.
- **Live reload.** It watches the file and only reloads maps whose pixels changed. Decoding runs on a background thread, so Blender doesn't freeze.
- **As many layers per map as you like.** Stack any number of `_A` layers, or groups, and they flatten into one Albedo map with normal blending and layer opacity.
- **Masks and custom maps.** Any suffix PSD Live doesn't know becomes its own map, named after the group or layer: `Blood_Mask`, `Dirt_Mask`, `Wet_Gloss`...
- **Nothing to install besides the add-on.** It uses the OpenImageIO library that ships with Blender. 8, 16 and 32-bit documents are supported.

## Install

Requires Blender 4.2 or newer. It was developed on Blender 5.2 on Windows.

1. Download `psd_live-<version>.zip` from the [latest release](https://github.com/kansai-gaijin/psd-live/releases/latest).
2. Drag the zip onto the Blender window, or use **Edit → Preferences → Get Extensions → Install from Disk**.

To build the zip yourself from a clone instead:
```bash
blender --command extension build --source-dir psd-live --output-dir .
```

## Use

1. In the Shader Editor, use **Add → PSD Layers (Live)** and pick your `.psd`. A **PSD Layers** node appears with one pin per map.
2. Connect the pins, for example **Albedo → Base Color** and **Normal → Normal Map → Normal**.
3. Paint in Photoshop and press **Ctrl+S**. The node's status line shows when it last updated.

The node also has:
- **Live** to pause watching.
- **Reload** to re-read every map now.
- **Edit** (pencil) to open the PSD in its default app.
- **Interpolation**, in the sidebar.

The **PSD** tab in the Shader Editor and Image Editor sidebars lists every linked PSD. From the Image Editor's **Image** menu you can link a PSD without adding a node; its maps still appear as images.

## Naming layers

The text after the **last underscore** of a layer or group name decides which map the layer belongs to. Case doesn't matter.

| Suffix | Map | Colour space | Where nothing is painted |
|---|---|---|---|
| `_A`, `_Albedo`, `_BC`, `_BaseColor`, `_Color` | **Albedo** | sRGB | transparent |
| `_N`, `_Normal` | **Normal** | Non-Color | flat normal (128, 128, 255) |
| `_R`, `_Roughness` | **Roughness** | Non-Color | black, alpha 0 |
| `_M`, `_Metallic` | **Metallic** | Non-Color | black, alpha 0 |
| `_S`, `_Specular` | **Specular** | Non-Color | black, alpha 0 |
| `_AO` | **AO** | Non-Color | black, alpha 0 |
| `_H`, `_Height` | **Height** | Non-Color | black, alpha 0 |
| `_E`, `_Emissive`, `_Emission` | **Emissive** | sRGB | transparent |
| anything else, e.g. `Blood_Mask` | a map named after the **whole name**: `Blood_Mask` | Non-Color | black, alpha 0 |

**What counts:**
- **Layers without a suffix are ignored.** Reference images, notes and generative-fill layers with long auto names stay out of your maps until you rename them or move them into a suffixed group.
- **Groups pass their suffix down.** Every layer inside a group named `Skin_A` counts as albedo unless it has a suffix of its own, and the innermost suffixed group wins.
- **Unknown suffixes keep their full name.** `Blood_Mask` and `Dirt_Mask` are two separate pins. Spaces become underscores, so a group named `Dirt Splat_Mask` gives the pin `Dirt_Splat_Mask`. Two groups with exactly the same name merge into one map.
- **A layer's eye decides whether it counts.** Hidden layers are left out of the map.
- **A group's eye and opacity only change the Photoshop view.** Hide the Normal group to look at your albedo and the normal map still reaches Blender.

### An example document

```
UV Guide            (no suffix, so ignored; handy as a hidden overlay)
Blood_Mask          group: its layers become the "Blood_Mask" map
Albedo_A            group, visible
  ├ Freckles        (inherits _A, 60 % opacity)
  └ Base skin       (inherits _A)
Normal_N            group, hidden in Photoshop but still exported
  └ Skin normal
Roughness_R
  └ Skin roughness
```

This gives four pins: **Albedo**, **Normal**, **Roughness** and **Blood_Mask**.

## How it works

- **Background thread:** reads the PSD and flattens each map bottom to top: normal blending, layer opacity, hidden layers skipped. Each map is written as a full-canvas PNG (EXR for 32-bit documents) into `<psd name>_maps/` next to the PSD. Only maps whose pixels changed are rewritten.
- **The images:** ordinary file images named `<psd name>_<Map>`, so they save with your .blend and work after reopening without re-reading the PSD.
- **The node:** a custom group node whose internal tree holds one Image Texture per map. It uses the mesh's UVs, or whatever you plug into its **Vector** input.
- **Watching:** a timer checks the PSD's modified time twice a second and re-reads once a save has settled. An update covers all maps, so a document with several 4K maps takes a few seconds to come through.
- **Normal maps** are re-normalised after blending. A map made of a single opaque layer is passed through untouched, bit for bit.

## Limitations

- **Normal blending only.** Blend modes, layer masks, clipping masks, layer effects and adjustment layers are ignored. Each layer contributes its own pixels at its own opacity. Smart objects and text layers work through the rasterised pixels Photoshop stores.
- **Normal maps** are blended like colour and then re-normalised. That's fine for painting, but it isn't a dedicated detail-normal blend.
- **The node needs the add-on installed to render.** Bake your final textures before handing a .blend to someone without it.
- **One image per map.** UDIM tiles aren't supported, but images repeat, so a single tile on UDIM 1001 or 1002 still lines up.
- **Large PSB documents** should work, since the file parser and OpenImageIO both support PSB, but they haven't been tested.

## License

GPL-3.0-or-later, like Blender itself. See [LICENSE](LICENSE).
