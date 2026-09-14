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


if __name__ == "__main__":
    unittest.main()
