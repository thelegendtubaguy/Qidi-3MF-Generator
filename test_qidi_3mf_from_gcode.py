import argparse
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from qidi_3mf_from_gcode import (
    model_settings_xml,
    parse_config_settings,
    parse_float,
    parse_gcode_metadata,
    parse_prediction_seconds,
    plate_json,
    qidi_package_gcode_bytes,
    positive_int_arg,
    read_thumbnail,
    slice_info_xml,
)


class FilamentMetadataTest(unittest.TestCase):
    def test_prunes_unused_defined_filament_and_preserves_tool_numbers(self):
        gcode = """
T0
T2
T2
T3
; total layer number: 1
; nozzle_diameter = 0.4
; filament_colour = #5EA9FD;#498FBC;#FF362D;#5CF30F
; filament_type = PLA;PLA;PLA;PLA
; filament_ids = A;B;C;D
; filament_map = 7,8,9,10
; filament used [mm] = 204.27, 0.00, 190.36, 138.77
; filament used [g] = 0.64, 0.00, 0.60, 0.44
; total filament used [g] = 1.68
"""
        metadata = parse_gcode_metadata(gcode)

        self.assertEqual([filament.index for filament in metadata.filaments], [1, 3, 4])
        self.assertEqual([filament.color for filament in metadata.filaments], ["#5EA9FD", "#FF362D", "#5CF30F"])
        self.assertEqual(metadata.filament_sequence, (1, 3, 4))

        slice_xml = slice_info_xml(metadata, "test", "1")
        ET.fromstring(slice_xml)
        self.assertIn('<filament id="1"', slice_xml)
        self.assertNotIn('<filament id="2"', slice_xml)
        self.assertIn('<filament id="3"', slice_xml)
        self.assertIn('<filament id="4"', slice_xml)
        self.assertIn('filament_list="0 2 3"', slice_xml)

        self.assertIn('value="7 9 10"', model_settings_xml("1", metadata))

        plate = json.loads(plate_json(metadata))
        self.assertEqual(plate["filament_colors"], ["#5EA9FD", "#FF362D", "#5CF30F"])
        self.assertEqual(plate["filament_ids"], [0, 2, 3])
        self.assertEqual(plate["first_extruder"], 0)

    def test_usage_summary_filters_when_tool_sequence_is_absent(self):
        gcode = """
; filament_colour = #111111;#222222;#333333
; filament_type = PLA;PETG;ABS
; filament used [mm] = 0.00, 100.00, 0.00
; filament used [g] = 0.00, 0.30, 0.00
"""
        metadata = parse_gcode_metadata(gcode)

        self.assertEqual([filament.index for filament in metadata.filaments], [2])
        self.assertEqual(metadata.filaments[0].filament_type, "PETG")
        self.assertEqual(json.loads(plate_json(metadata))["first_extruder"], 1)

    def test_implicit_t0_usage_is_kept_with_later_explicit_tool_change(self):
        gcode = """
T1
; filament_colour = #111111;#222222
; filament_type = PLA;PETG
; filament used [mm] = 50.00, 100.00
; filament used [g] = 0.15, 0.30
"""
        metadata = parse_gcode_metadata(gcode)

        self.assertEqual([filament.index for filament in metadata.filaments], [1, 2])
        self.assertEqual(metadata.filament_sequence, (1, 2))
        self.assertEqual(json.loads(plate_json(metadata))["first_extruder"], 0)

    def test_quoted_semicolon_filament_values_are_split_cleanly(self):
        gcode = """
; filament_colour = "#111111";"#222222"
; filament_type = "PLA";"PETG"
; filament_ids = "A";"B"
"""
        metadata = parse_gcode_metadata(gcode)

        self.assertEqual([filament.filament_type for filament in metadata.filaments], ["PLA", "PETG"])
        self.assertEqual([filament.color for filament in metadata.filaments], ["#111111", "#222222"])
        self.assertEqual([filament.filament_id for filament in metadata.filaments], ["A", "B"])


class ParserRobustnessTest(unittest.TestCase):
    def test_settings_parser_skips_thumbnail_payload(self):
        settings = parse_config_settings(
            """
; thumbnail begin 150x150 12
; /76W83fHHkHzEBAAAAAAAAAAAAAAAAAAAAAAD06RHqI+QxahWdCAAAAABJRU5ErkJggg=
; thumbnail end
; filament_type = PLA
"""
        )

        self.assertEqual(settings, {"filament_type": "PLA"})

    def test_m73_prediction_is_order_independent(self):
        self.assertEqual(parse_prediction_seconds("M73 R12 P0\n"), 720)
        self.assertEqual(parse_prediction_seconds("M73 P0 R12\n"), 720)

    def test_first_layer_estimate_is_used(self):
        metadata = parse_gcode_metadata("; estimated first layer printing time (normal mode) = 5s\n")

        self.assertEqual(metadata.first_layer_time, 5.0)

    def test_comment_object_bbox_tracks_modal_xy(self):
        metadata = parse_gcode_metadata(
            """
; printing object Cube id:0 copy 0
G1 X10 Y20
G1 X15
G1 Y25
; stop printing object Cube id:0 copy 0
"""
        )

        self.assertEqual(metadata.objects[0].bbox, (10.0, 20.0, 15.0, 25.0))

    def test_exclude_object_parser_handles_quoted_name_and_spaced_polygon(self):
        metadata = parse_gcode_metadata('EXCLUDE_OBJECT_DEFINE NAME="Foo Bar" POLYGON=[[1, 2], [3, 4], [1, 4]]\n')

        self.assertEqual(metadata.objects[0].name, "Foo Bar")
        self.assertEqual(metadata.objects[0].bbox, (1.0, 2.0, 3.0, 4.0))

    def test_plate_index_rejects_non_positive_integers(self):
        self.assertEqual(positive_int_arg("02"), "2")
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_int_arg("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_int_arg("abc")

    def test_read_thumbnail_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.png"
            path.write_bytes(b"not a png")
            with self.assertRaises(ValueError):
                read_thumbnail(path)

    def test_non_finite_float_values_are_rejected(self):
        self.assertEqual(parse_float("nan", 1.2), 1.2)
        self.assertEqual(parse_float("inf", 1.2), 1.2)

    def test_package_gcode_injects_exclude_object_defines_before_executable_block(self):
        gcode = b"""; HEADER_BLOCK_START
; HEADER_BLOCK_END

; EXECUTABLE_BLOCK_START
G28
; printing object Foo Bar id:0 copy 0
G1 X10 Y20
G1 X15 Y25
; stop printing object Foo Bar id:0 copy 0
; enable_prime_tower = 1
; wipe_tower_x = 100
; wipe_tower_y = 120
; prime_tower_width = 20
"""
        metadata = parse_gcode_metadata(gcode.decode())
        packaged = qidi_package_gcode_bytes(gcode, metadata).decode()

        self.assertLess(packaged.index("EXCLUDE_OBJECT_DEFINE NAME=Foo_Bar_id_0_copy_0"), packaged.index("G28"))
        self.assertIn("EXCLUDE_OBJECT_DEFINE NAME=wipe_tower_area CENTER=110,130 POLYGON=[[100,120],[120,120],[120,140],[100,140],[100,120]]", packaged)

    def test_package_gcode_does_not_duplicate_existing_exclude_object_defines(self):
        gcode = b"EXCLUDE_OBJECT_DEFINE NAME=already CENTER=1,1 POLYGON=[[0,0],[1,0],[1,1],[0,1],[0,0]]\nG28\n"
        metadata = parse_gcode_metadata(gcode.decode())

        self.assertEqual(qidi_package_gcode_bytes(gcode, metadata), gcode)


if __name__ == "__main__":
    unittest.main()
