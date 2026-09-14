from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import extract_blocks  # noqa: E402
import extract_tables  # noqa: E402
import build_dual  # noqa: E402
import debug_flow  # noqa: E402

try:
    import fitz
except ImportError:  # pragma: no cover - CI installs the runtime dependency
    fitz = None


@unittest.skipIf(fitz is None, "PyMuPDF is required for PDF integration fixtures")
class PdfLayoutIntegrationTests(unittest.TestCase):
    def test_cross_page_paragraph_sets_continuation_metadata(self):
        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            source = td / "cross-page.pdf"
            blocks_path = td / "blocks.json"
            doc = fitz.open()
            first = doc.new_page(width=600, height=800)
            first.insert_textbox(
                fitz.Rect(40, 700, 270, 750),
                "This paragraph continues onto the next", fontsize=10)
            second = doc.new_page(width=600, height=800)
            second.insert_textbox(
                fitz.Rect(40, 60, 270, 110),
                "page without starting a new paragraph.", fontsize=10)
            doc.save(source)
            doc.close()

            self.assertEqual(extract_blocks.main([
                "--input", str(source), "--output", str(blocks_path)]), 0)
            pages = json.loads(blocks_path.read_text(encoding="utf-8"))["pages"]
            self.assertTrue(pages[0]["blocks"][-1]["continues_to_next"])
            self.assertTrue(pages[1]["blocks"][0]["continues_from_prev"])

    def test_wide_raster_image_is_recorded_as_barrier(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False)
        pixmap.clear_with(220)
        page.insert_image(fitz.Rect(80, 250, 520, 400), pixmap=pixmap)
        barriers = extract_blocks.detect_layout_barriers(page, [])
        self.assertTrue(any(item["kind"] == "image" for item in barriers))
        doc.close()

    def test_wide_vector_figure_partitions_two_column_flow(self):
        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            source = td / "figure.pdf"
            blocks_path = td / "blocks.json"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_textbox(fitz.Rect(40, 80, 270, 115), "left top", fontsize=10)
            page.insert_textbox(fitz.Rect(330, 80, 560, 115), "right top", fontsize=10)
            page.draw_rect(fitz.Rect(80, 260, 520, 410), color=(0, 0, 0),
                           fill=(0.9, 0.9, 0.9))
            page.insert_textbox(fitz.Rect(40, 500, 270, 535), "left bottom", fontsize=10)
            page.insert_textbox(fitz.Rect(330, 500, 560, 535), "right bottom", fontsize=10)
            doc.save(source)
            doc.close()

            self.assertEqual(extract_blocks.main([
                "--input", str(source), "--output", str(blocks_path)]), 0)
            data = json.loads(blocks_path.read_text(encoding="utf-8"))
            page_data = data["pages"][0]
            self.assertTrue(any(item["kind"] == "vector"
                                for item in page_data["layout_barriers"]))
            self.assertEqual([block["text"] for block in page_data["blocks"]],
                             ["left top", "right top", "left bottom", "right bottom"])

            overlay_path = td / "flow-overlay.pdf"
            self.assertEqual(debug_flow.main([
                "--source", str(source), "--blocks", str(blocks_path),
                "--out", str(overlay_path),
            ]), 0)
            self.assertGreater(overlay_path.stat().st_size, 0)

            translations_path = td / "translations.json"
            output_path = td / "dual.pdf"
            translations = {
                block["id"]: {
                    "zh": f"translated {block['text']}",
                    "source_hash": block["source_hash"],
                    "layout_uid": block["layout_uid"],
                }
                for block in page_data["blocks"]
            }
            translations_path.write_text(json.dumps(translations), encoding="utf-8")
            self.assertEqual(build_dual.main([
                "--source", str(source), "--blocks", str(blocks_path),
                "--translations", str(translations_path), "--output", str(output_path),
            ]), 0)
            output = fitz.open(output_path)
            self.assertEqual(output.page_count, 1)
            self.assertEqual(output[0].rect.width, 1200)
            output.close()

    def test_multiline_table_caption_is_not_a_table_cell(self):
        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            source = td / "table.pdf"
            tables_path = td / "tables.json"
            blocks_path = td / "blocks.json"
            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_textbox(
                fitz.Rect(40, 90, 560, 145),
                "Table 1. Applications of\nLLM4RL.", fontsize=10)
            for y in (180, 220, 260):
                page.draw_line((40, y), (560, y), width=0.8)
            page.insert_textbox(fitz.Rect(50, 184, 250, 215), "Method", fontsize=9)
            page.insert_textbox(fitz.Rect(350, 184, 540, 215), "Score", fontsize=9)
            page.insert_textbox(fitz.Rect(50, 224, 250, 255), "Baseline", fontsize=9)
            page.insert_textbox(fitz.Rect(350, 224, 540, 255), "0.9", fontsize=9)
            doc.save(source)
            doc.close()

            self.assertEqual(extract_tables.main([
                "--input", str(source), "--output", str(tables_path)]), 0)
            tables = json.loads(tables_path.read_text(encoding="utf-8"))
            table = tables["pages"][0]["tables"][0]
            cell_text = " ".join(cell["text"] for cell in table["cells"])
            self.assertNotIn("Applications", cell_text)
            self.assertNotIn("LLM4RL", cell_text)
            self.assertGreaterEqual(table["region"][1], 179)

            self.assertEqual(extract_blocks.main([
                "--input", str(source), "--output", str(blocks_path),
                "--tables", str(tables_path)]), 0)
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))["pages"][0]["blocks"]
            captions = [block for block in blocks if block["kind"] == "table_caption"]
            self.assertEqual(len(captions), 1)
            self.assertIn("Applications of LLM4RL", captions[0]["text"])


if __name__ == "__main__":
    unittest.main()
