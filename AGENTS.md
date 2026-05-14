# AGENTS.md

## Project scope

`qidi_3mf_from_gcode.py` converts OrcaSlicer `.gcode` files into QIDI Studio-style print-ready `.3mf` packages for QIDI Max 4 printer workflows.

Use QIDI Studio-produced `.3mf` files as the structural baseline for generated output when fixtures are available locally. Do not assume local fixture names are committed or stable.

## Validation commands

```bash
python3 -m py_compile qidi_3mf_from_gcode.py
python3 qidi_3mf_from_gcode.py input.gcode -o /tmp/output.3mf --force
unzip -t /tmp/output.3mf
zipinfo -1 /tmp/output.3mf | sort
unzip -p /tmp/output.3mf Metadata/slice_info.config
unzip -p /tmp/output.3mf Metadata/model_settings.config
unzip -p /tmp/output.3mf Metadata/plate_1.json | python3 -m json.tool
unzip -p /tmp/output.3mf Metadata/filament_sequence.json | python3 -m json.tool
```

Compare against a QIDI Studio `.3mf` fixture when one is available locally:

```bash
unzip -l reference.gcode.3mf
unzip -p reference.gcode.3mf Metadata/slice_info.config
unzip -p reference.gcode.3mf Metadata/model_settings.config
unzip -p reference.gcode.3mf Metadata/plate_1.json | python3 -m json.tool
unzip -p reference.gcode.3mf Metadata/filament_sequence.json | python3 -m json.tool
```

## QIDI Studio package structure

QIDI Studio print-ready `.3mf` packages contain 16 entries:

```text
[Content_Types].xml
_rels/.rels
3D/3dmodel.model
Metadata/_rels/model_settings.config.rels
Metadata/cut_information.xml
Metadata/filament_sequence.json
Metadata/model_settings.config
Metadata/pick_1.png
Metadata/plate_1.gcode
Metadata/plate_1.gcode.md5
Metadata/plate_1.json
Metadata/plate_1.png
Metadata/plate_no_light_1.png
Metadata/project_settings.config
Metadata/slice_info.config
Metadata/top_1.png
```

Generated packages should keep this entry shape unless a printer test proves an entry is unnecessary.

`Auxiliaries/.thumbnails/*` is omitted because QIDI Studio print-ready `.3mf` packages generated for direct printing do not require those paths.

## Time remaining metadata

QIDI Studio `.3mf` carries print duration in `Metadata/slice_info.config`:

```xml
<metadata key="prediction" value="212185"/>
```

`prediction` is seconds.

Orca `.gcode` can contain `M73 P... R...` commands, but QIDI Max 4 Klipper's `/home/qidi/klipper/klippy/extras/display_status.py` reads only `M73 P` and ignores `M73 R`. Screen time remaining for `.3mf` prints is expected to depend on `Metadata/slice_info.config` and QIDI package metadata, not Klipper `M73 R`.

`qidi_3mf_from_gcode.py` derives `prediction` from `; estimated printing time (...) = ...` first, then falls back to earliest/maximum low-progress `M73 R` minutes.

## Multicolor metadata

QIDI Studio two-filament `Metadata/slice_info.config` uses:

```xml
<metadata key="filament_maps" value="1 1"/>
<metadata key="limit_filament_maps" value="0 0"/>
<layer_filament_list filament_list="0 1" layer_ranges="0 389" />
```

`Metadata/model_settings.config` uses:

```xml
<metadata key="filament_map_mode" value="Auto For Flush"/>
<metadata key="filament_maps" value="1 1"/>
<metadata key="filament_volume_maps" value="0 0"/>
```

`filament_maps` comes from Orca/QIDI setting `filament_map`. If missing, generate one `1` per filament.

`limit_filament_maps` and `filament_volume_maps` should contain one `0` per filament.

`layer_filament_list filament_list` uses zero-based filament indices and should include every filament used on the plate, e.g. `0 1` for two colors across the full layer range.

## Filament entries

QIDI Studio filament entries include these attributes:

```xml
<filament id="1" tray_info_idx="QD_3_1_1" type="PLA" color="#E2DFCD" used_m="38.33" used_g="114.33" used_for_object="true" used_for_support="false" group_id="0" nozzle_diameter="0.20" volume_type="Standard"/>
```

Generated entries should include the same attributes. `tray_info_idx` comes from `filament_ids` when present. `volume_type` comes from `nozzle_volume_type` or `default_nozzle_volume_type`, defaulting to `Standard`.

Orca may encode multivalue fields with semicolons instead of comma-separated JSON-style lists in the G-code footer:

```text
; filament_colour = #00C1AE;#498FBC
; filament_type = PLA;PLA
; filament_ids = OGFPM002;OGFPM002
; filament_vendor = Polymaker;Polymaker
; filament_settings_id = "...";"..."
```

The parser must split both comma-separated settings and semicolon-separated settings.

Orca per-filament usage can use comma-separated values:

```text
; filament used [mm] = 11640.28, 10849.47
; filament used [g] = 36.68, 34.19
```

`used_m` in `slice_info.config` is meters, so `filament used [mm]` must be divided by `1000`.

## Object metadata

QIDI Studio `slice_info.config` lists printable objects only, not the wipe tower:

```xml
<object identify_id="53" name="Cube_id_0_copy_0" skipped="false" />
<object identify_id="89" name="Cube_id_1_copy_0" skipped="false" />
```

`Metadata/plate_1.json` includes printable objects and the wipe tower:

```json
{"id":1000,"name":"wipe_tower"}
```

`qidi_3mf_from_gcode.py` should:

- prefer `EXCLUDE_OBJECT_DEFINE` polygons when present;
- otherwise derive object names and bounding boxes from Orca `; printing object ...` / `; stop printing object ...` sections and `G0`/`G1`/`G2`/`G3` XY moves;
- normalize object names to QIDI-style names such as `Cube_id_0_copy_0`;
- add a `wipe_tower` object to `plate_1.json` when `enable_prime_tower=1`, using `wipe_tower_x`, `wipe_tower_y`, and `prime_tower_width`;
- assign wipe tower id `1000`.

`Metadata/cut_information.xml` should include printable objects and exclude `wipe_tower`:

```xml
<object id="1">
  <cut_id id="0" check_sum="1" connectors_cnt="0"/>
</object>
```

## Filament sequence

QIDI Studio writes `Metadata/filament_sequence.json`:

```json
{"plate_1":{"sequence":[1,2,1,2]}}
```

Sequence values are one-based filament/tool numbers. `T0` maps to `1`, `T1` maps to `2`.

Generated sequence should collapse consecutive duplicate `T#` commands.

QIDI Studio sequence length is one initial tool plus one entry per tool change; an alternating-color print usually has one `filament_sequence.json` entry per emitted `T#` transition after consecutive duplicates are collapsed.

## Thumbnail metadata

Orca embeds PNG thumbnails in G-code:

```gcode
; thumbnail begin 150x150 1708
; iVBOR...
; thumbnail end
```

`qidi_3mf_from_gcode.py` should extract the largest embedded PNG and use it for:

```text
Metadata/plate_1.png
Metadata/plate_no_light_1.png
Metadata/top_1.png
Metadata/pick_1.png
```

`--thumbnail path.png` overrides embedded thumbnails.

Fallback thumbnail is a 1x1 transparent PNG.

## MD5

`Metadata/plate_1.gcode.md5` is the uppercase MD5 hex digest of `Metadata/plate_1.gcode` bytes.

Validate with:

```bash
python3 - <<'PY'
import hashlib, zipfile
with zipfile.ZipFile('/tmp/output.3mf') as z:
    g = z.read('Metadata/plate_1.gcode')
    m = z.read('Metadata/plate_1.gcode.md5').decode()
print(hashlib.md5(g).hexdigest().upper() == m, m)
PY
```

## XML parsing

Every generated XML entry must parse with `xml.etree.ElementTree.fromstring`:

```bash
python3 - <<'PY'
import zipfile, xml.etree.ElementTree as ET
with zipfile.ZipFile('/tmp/output.3mf') as z:
    for name in ['Metadata/slice_info.config','Metadata/model_settings.config','Metadata/cut_information.xml','3D/3dmodel.model','_rels/.rels','Metadata/_rels/model_settings.config.rels']:
        ET.fromstring(z.read(name))
        print('ok', name)
PY
```

## Known unresolved points

`first_layer_time` is present in QIDI Studio `Metadata/slice_info.config` and `Metadata/plate_1.json`. Orca G-code may not expose this value, so generated output currently uses `0` when unavailable.

QIDI Studio object `identify_id` values are not always sequential and may not match `plate_1.json` object ids. Generated output uses sequential ids for printable objects unless `EXCLUDE_OBJECT_DEFINE` supplies stronger identifiers.

QIDI Studio `plate_1.json` wipe tower bbox appears based on sliced tower geometry, while generated output approximates it from `wipe_tower_x`, `wipe_tower_y`, and `prime_tower_width`.
