# QIDI 3MF generator

`qidi_3mf_from_gcode.py` wraps an OrcaSlicer `.gcode` file in a QIDI-style print-ready `.3mf` package.

```bash
python3 qidi_3mf_from_gcode.py input.gcode
python3 qidi_3mf_from_gcode.py input.gcode -o output.gcode.3mf --force
```

The package includes:

- `Metadata/plate_1.gcode`
- `Metadata/plate_1.gcode.md5`
- `Metadata/slice_info.config` with `prediction` derived from `; estimated printing time (...) = ...` or `M73 R...`
- `Metadata/slice_info.config` filament entries from Orca per-filament usage, color, and material settings
- `Metadata/model_settings.config` with QIDI-style `filament_maps` and `filament_volume_maps`
- `Metadata/filament_sequence.json` from generated `T#` commands
- `Metadata/plate_1.json` from `EXCLUDE_OBJECT_DEFINE` polygons when present, or object bounding boxes derived from Orca `; printing object ...` sections
- PNG thumbnails extracted from OrcaSlicer `; thumbnail begin ...` G-code blocks, with a 1x1 fallback unless `--thumbnail path.png` is supplied

The script uses only Python standard-library modules.
