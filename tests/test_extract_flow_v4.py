import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("extract_blocks", ROOT / "scripts" / "extract_blocks.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def blk(text, y0, y1, x0=50, x1=250, size=10, column="left", kind="body"):
    return {
        "id": "x", "_page": 1, "bbox": [x0, y0, x1, y1], "size": size,
        "font": "Times", "bold": False, "heading": False, "color": 0,
        "math_only": False, "has_math": False, "chars": len(text), "text": text,
        "kind": kind, "column": column, "source_hash": "x",
    }


class FlowV4Tests(unittest.TestCase):
    def test_source_line_records_keep_span_geometry(self):
        raw = {"lines": [{
            "bbox": [10, 20, 100, 32],
            "spans": [{"text": "left ", "bbox": [10, 20, 40, 32]},
                      {"text": "right", "bbox": [60, 20, 100, 32]}],
        }]}
        records = M.source_line_records(raw)
        self.assertEqual(records[0]["text"], "left right")
        self.assertEqual(records[0]["spans"][1]["bbox"], [60.0, 20.0, 100.0, 32.0])

    def test_tight_blocks_merge_even_after_period(self):
        self.assertTrue(M.can_merge(
            blk("This is one sentence.", 100, 110),
            blk("This is still the same paragraph.", 113, 123),
        ))

    def test_indented_new_paragraph_is_not_merged(self):
        self.assertFalse(M.can_merge(
            blk("Paragraph one ends here.", 100, 110, x0=50),
            blk("New paragraph starts here.", 117, 127, x0=62),
        ))

    def test_different_columns_never_merge(self):
        self.assertFalse(M.can_merge(
            blk("left", 100, 110, column="left"),
            blk("right", 112, 122, x0=350, x1=550, column="right"),
        ))

    def test_table_never_merges_with_body(self):
        self.assertFalse(M.can_merge(
            blk("body", 100, 110),
            blk("table row", 112, 122, kind="table"),
        ))

    def test_flow_left_before_right_in_zone(self):
        left = blk("left", 100, 110, column="left")
        right = blk("right", 90, 100, x0=350, x1=550, column="right")
        out = M.assign_flow([right, left], [], 600)
        self.assertEqual([x["text"] for x in out], ["left", "right"])

    def test_hash_changes_when_text_changes(self):
        self.assertNotEqual(
            M.source_hash(1, [1, 2, 3, 4], "abc"),
            M.source_hash(1, [1, 2, 3, 4], "abd"),
        )

    def test_captions_are_classified(self):
        bbox = [40, 100, 260, 120]
        args = (bbox, 600, 800, False, "Times", False, [])
        self.assertEqual(M.classify_kind("Table 1. Results", *args), "table_caption")
        self.assertEqual(M.classify_kind("Fig. 2. Architecture", *args), "figure_caption")
        self.assertEqual(
            M.classify_kind("Table 1. Results", *args[:-1], [[30, 90, 570, 300]]),
            "table_caption",
        )

    def test_full_width_raw_block_is_split_by_physical_line_column(self):
        raw = {
            "type": 0,
            "bbox": [40, 70, 560, 105],
            "lines": [
                {"bbox": [40, 70, 270, 82], "spans": [{"text": "left"}]},
                {"bbox": [330, 70, 560, 82], "spans": [{"text": "right"}]},
            ],
        }
        parts = M.split_raw_block(raw, 600)
        self.assertEqual(len(parts), 2)
        self.assertEqual([M.block_text(part) for part in parts], ["left", "right"])
        self.assertEqual([M.classify_column(part["bbox"], 600) for part in parts],
                         ["left", "right"])

    def test_same_column_raw_lines_keep_indented_paragraphs_separate(self):
        raw = {
            "type": 0, "bbox": [40, 70, 560, 140],
            "lines": [
                {"bbox": [40, 70, 270, 82], "spans": [{"text": "First paragraph."}]},
                {"bbox": [52, 85, 270, 97], "spans": [{"text": "Second paragraph."}]},
                {"bbox": [330, 70, 560, 82], "spans": [{"text": "Right column."}]},
            ],
        }
        parts = M.split_raw_block(raw, 600)
        left = [part for part in parts if M.classify_column(part["bbox"], 600) == "left"]
        self.assertEqual([M.block_text(part) for part in left],
                         ["First paragraph.", "Second paragraph."])

    def test_table_edge_splits_caption_from_header_in_same_raw_block(self):
        raw = {
            "type": 0, "bbox": [40, 90, 560, 210],
            "lines": [
                {"bbox": [40, 90, 300, 102], "spans": [{"text": "Table 1. Results"}]},
                {"bbox": [40, 106, 300, 118], "spans": [{"text": "continued caption"}]},
                {"bbox": [40, 145, 560, 157], "spans": [{"text": "Method Score"}]},
                {"bbox": [40, 195, 560, 207], "spans": [{"text": "body below"}]},
            ],
        }
        parts = M.split_at_table_boundaries(raw, [[40, 140, 560, 180]])
        self.assertEqual([M.block_text(part) for part in parts], [
            "Table 1. Results continued caption", "Method Score", "body below"])

    def test_left_table_boundaries_do_not_split_right_column_paragraph(self):
        raw = {
            "type": 0, "bbox": [330, 90, 560, 210],
            "lines": [
                {"bbox": [330, 90, 560, 102], "spans": [{"text": "right one"}]},
                {"bbox": [330, 145, 560, 157], "spans": [{"text": "right two"}]},
                {"bbox": [330, 195, 560, 207], "spans": [{"text": "right three"}]},
            ],
        }
        parts = M.split_at_table_boundaries(raw, [[40, 140, 280, 180]])
        self.assertEqual(len(parts), 1)
        self.assertEqual(M.block_text(parts[0]), "right one right two right three")

    def test_wide_layout_barrier_splits_reading_zones(self):
        blocks = [
            blk("left top", 80, 100),
            blk("right top", 80, 100, x0=350, x1=550, column="right"),
            blk("left bottom", 500, 520),
            blk("right bottom", 500, 520, x0=350, x1=550, column="right"),
        ]
        out = M.assign_flow(
            blocks, [{"kind": "image", "bbox": [80, 250, 520, 400]}], 600)
        self.assertEqual([item["text"] for item in out],
                         ["left top", "right top", "left bottom", "right bottom"])
        self.assertTrue(out[2]["flow_break"])

    def test_adjacent_vector_parts_form_one_wide_barrier(self):
        clusters = M.cluster_object_rects([
            [60, 200, 260, 300], [280, 200, 520, 300],
        ])
        self.assertEqual(clusters, [[60.0, 200.0, 520.0, 300.0]])

    def test_heading_at_page_boundary_prevents_false_continuation(self):
        previous = {"page": 1, "blocks": [
            {**blk("unfinished sentence", 700, 720), "flow_index": 0},
        ]}
        following = {"page": 2, "blocks": [
            {**blk("New section", 40, 60, kind="heading"), "flow_index": 0},
            {**blk("body starts here", 70, 90), "flow_index": 1},
        ]}
        M.mark_page_continuations([previous, following])
        self.assertNotIn("continues_to_next", previous["blocks"][0])
        self.assertNotIn("continues_from_prev", following["blocks"][1])

    def test_meta_at_page_boundary_does_not_block_continuation(self):
        previous = {"page": 1, "blocks": [
            {**blk("unfinished sentence", 700, 720), "flow_index": 0},
            {**blk("12", 780, 790, kind="meta"), "flow_index": 1},
        ]}
        following = {"page": 2, "blocks": [
            {**blk("Journal header", 10, 20, kind="meta"), "flow_index": 0},
            {**blk("continues here.", 40, 60), "flow_index": 1},
        ]}
        M.mark_page_continuations([previous, following])
        self.assertTrue(previous["blocks"][0]["continues_to_next"])
        self.assertTrue(following["blocks"][1]["continues_from_prev"])

    def test_page_edge_image_barrier_prevents_continuation(self):
        previous = {"page": 1, "layout_barriers": [
            {"kind": "image", "bbox": [50, 730, 550, 790]},
        ], "blocks": [
            {**blk("unfinished sentence", 680, 710), "flow_index": 0},
        ]}
        following = {"page": 2, "blocks": [
            {**blk("continues here.", 40, 60), "flow_index": 0},
        ]}
        M.mark_page_continuations([previous, following])
        self.assertNotIn("continues_to_next", previous["blocks"][0])

    def test_first_body_flow_break_prevents_continuation(self):
        previous = {"page": 1, "blocks": [
            {**blk("unfinished sentence", 680, 710), "flow_index": 0},
        ]}
        following = {"page": 2, "blocks": [
            {**blk("new text.", 100, 120), "flow_index": 0, "flow_break": True},
        ]}
        M.mark_page_continuations([previous, following])
        self.assertNotIn("continues_to_next", previous["blocks"][0])


if __name__ == "__main__":
    unittest.main()
