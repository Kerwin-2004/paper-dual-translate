from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import extract_blocks  # noqa: E402
import extract_tables  # noqa: E402
import audit_nested_blocks  # noqa: E402
import auto_translate  # noqa: E402
import build_dual  # noqa: E402
import debug_flow  # noqa: E402
import provenance  # noqa: E402

try:
    import fitz
except ImportError:  # pragma: no cover - CI installs the runtime dependency
    fitz = None


@unittest.skipIf(fitz is None, "PyMuPDF is required for PDF integration fixtures")
class PdfLayoutIntegrationTests(unittest.TestCase):
    def test_auto_translation_preserves_nested_math_in_built_pdf(self):
        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            source = td / "nested.pdf"
            blocks_path = td / "blocks.json"
            translations_path = td / "translations.json"
            output_path = td / "dual.pdf"

            doc = fitz.open()
            page = doc.new_page(width=600, height=800)
            page.insert_text((40, 110), "The controller uses an embedded equation.", fontsize=10)
            page.insert_text((250, 135), "E=mc^2", fontsize=10)
            doc.save(source)
            doc.close()

            common = {"page": 1, "column": "left", "size": 10, "font": "Times",
                      "bold": False, "heading": False, "color": 0, "has_math": False}
            blocks = {"schema_version": 4, "page_count": 1, "pages": [{
                "page": 1, "width": 600, "height": 800, "blocks": [
                    {**common, "id": "p1b0", "text": "The controller uses an embedded equation.",
                     "bbox": [35, 90, 565, 155], "kind": "body", "math_only": False,
                     "flow_index": 0},
                    {**common, "id": "p1b1", "text": "E=mc^2",
                     "bbox": [245, 120, 330, 145], "kind": "math_only", "math_only": True,
                     "has_math": True, "flow_index": 1},
                ], "layout_barriers": [],
            }]}
            blocks_path.write_text(json.dumps(blocks), encoding="utf-8")
            self.assertEqual(audit_nested_blocks.main([
                "--blocks", str(blocks_path), "--apply"]), 0)
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))
            provenance.refresh_block_identities(blocks)
            blocks_path.write_text(json.dumps(blocks), encoding="utf-8")

            def fake_call_api(cfg, messages, timeout, temperature):
                payload = json.loads(messages[-1]["content"])
                result = {}
                for item in payload:
                    markers = " ".join(part for part in item["en"].splitlines()
                                       if "PDT_INLINE" in part)
                    result[item["id"]] = "控制器使用嵌入方程 " + markers
                return json.dumps(result, ensure_ascii=False), {}

            with mock.patch.object(auto_translate, "call_api", side_effect=fake_call_api):
                self.assertEqual(auto_translate.main([
                    "--blocks", str(blocks_path), "--output", str(translations_path),
                    "--api-key", "fixture", "--workers", "1",
                ]), 0)
            translations = json.loads(translations_path.read_text(encoding="utf-8"))
            self.assertIn("E=mc^2", translations["p1b0"]["zh"])

            self.assertEqual(build_dual.main([
                "--source", str(source), "--blocks", str(blocks_path),
                "--translations", str(translations_path), "--output", str(output_path),
            ]), 0)
            output = fitz.open(output_path)
            right_text = output[0].get_text(clip=fitz.Rect(600, 0, 1200, 800))
            output.close()
            self.assertIn("E=mc", right_text)

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
                "Table 1. Applications of\nLLM4RL.\nMethod                         Score",
                fontsize=10)
            for y in (120, 160, 200):
                page.draw_line((40, y), (560, y), width=0.8)
            page.insert_textbox(fitz.Rect(50, 164, 250, 195), "Baseline", fontsize=9)
            page.insert_textbox(fitz.Rect(350, 164, 540, 195), "0.9", fontsize=9)
            doc.save(source)
            doc.close()

            self.assertEqual(extract_tables.main([
                "--input", str(source), "--output", str(tables_path)]), 0)
            tables = json.loads(tables_path.read_text(encoding="utf-8"))
            table = tables["pages"][0]["tables"][0]
            cell_text = " ".join(cell["text"] for cell in table["cells"])
            self.assertNotIn("Applications", cell_text)
            self.assertNotIn("LLM4RL", cell_text)
            self.assertIn("Method", cell_text)
            self.assertGreaterEqual(table["region"][1], 119)

            self.assertEqual(extract_blocks.main([
                "--input", str(source), "--output", str(blocks_path),
                "--tables", str(tables_path)]), 0)
            blocks = json.loads(blocks_path.read_text(encoding="utf-8"))["pages"][0]["blocks"]
            captions = [block for block in blocks if block["kind"] == "table_caption"]
            self.assertEqual(len(captions), 1)
            self.assertIn("Applications of LLM4RL", captions[0]["text"])
            self.assertNotIn("Method", captions[0]["text"])


if __name__ == "__main__":
    unittest.main()
