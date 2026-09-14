from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import auto_translate  # noqa: E402
import build_dual  # noqa: E402
import config  # noqa: E402
import extract_blocks  # noqa: E402
import extract_tables  # noqa: E402
import merge_paragraphs  # noqa: E402
import pipeline  # noqa: E402
import provenance  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.old_cfg = config._CFG
        self.old_fonts = list(config.FONT_CANDIDATES)
        self.old_bold = list(config.FONT_BOLD_CANDIDATES)
        self.old_math = list(config.MATH_FONT_CANDIDATES)

    def tearDown(self):
        config._CFG = self.old_cfg
        config.FONT_CANDIDATES[:] = self.old_fonts
        config.FONT_BOLD_CANDIDATES[:] = self.old_bold
        config.MATH_FONT_CANDIDATES[:] = self.old_math

    def test_configured_fonts_are_honored(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            normal = td / "normal.ttf"
            bold = td / "bold.ttf"
            math = td / "math.ttf"
            for p in (normal, bold, math):
                p.write_bytes(b"fixture")
            config.FONT_CANDIDATES[:] = []
            config.FONT_BOLD_CANDIDATES[:] = []
            config.MATH_FONT_CANDIDATES[:] = []
            config._CFG = {
                "font": str(normal),
                "font_bold": str(bold),
                "math_font": str(math),
            }
            self.assertEqual(config.find_font(), normal)
            self.assertEqual(config.find_font(bold=True), bold)
            self.assertEqual(config.find_math_font(), math)


class PipelineTests(unittest.TestCase):
    def test_safe_command_masks_api_key(self):
        rendered = pipeline._safe_cmd_for_log(
            ["python", "x.py", "--api-key", "sk-secret", "--workers", "2"])
        self.assertNotIn("sk-secret", rendered)
        self.assertIn("***REDACTED***", rendered)

    def test_run_step_passes_environment(self):
        fake = SimpleNamespace(returncode=0)
        env = {"PDT_API_KEY": "secret"}
        with mock.patch.object(pipeline.subprocess, "run", return_value=fake) as run:
            with redirect_stdout(io.StringIO()):
                rc = pipeline.run_step(Path(sys.executable), "x.py", [], "x", env=env)
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.kwargs["env"], env)

    def test_build_rejects_incomplete_prepare_marker(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / "source.pdf"
            source.write_bytes(b"fixture")
            work = td / "work"
            work.mkdir()
            (work / ".prepare-incomplete").write_text("incomplete", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                rc = pipeline.main([
                    "--source", str(source), "--mode", "build",
                    "--work-dir", str(work), "--python", sys.executable,
                ])
            self.assertEqual(rc, 2)

    def test_prepare_refreshes_identity_after_mutating_steps(self):
        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            source = td / "source.pdf"
            source.write_bytes(b"fixture")
            work = td / "work"

            def fake_step(py_exe, script_name, args, desc, env=None):
                if script_name == "extract_tables.py":
                    out = Path(args[args.index("--output") + 1])
                    out.write_text('{"pages": []}', encoding="utf-8")
                elif script_name == "extract_blocks.py":
                    out = Path(args[args.index("--output") + 1])
                    block = {"id": "p1b0", "text": "same", "column": "left",
                             "bbox": [10, 20, 200, 40]}
                    block.update(provenance.block_identity(1, block))
                    out.write_text(
                        json.dumps({"schema_version": 4, "pages": [
                            {"page": 1, "blocks": [block]}]}), encoding="utf-8")
                elif script_name == "audit_nested_blocks.py":
                    path = Path(args[args.index("--blocks") + 1])
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["pages"][0]["blocks"][0]["bbox"][2] = 260
                    path.write_text(json.dumps(data), encoding="utf-8")
                return 0

            check = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(pipeline.subprocess, "run", return_value=check), \
                    mock.patch.object(pipeline, "run_step", side_effect=fake_step), \
                    redirect_stdout(io.StringIO()):
                rc = pipeline.main([
                    "--source", str(source), "--mode", "prepare",
                    "--work-dir", str(work), "--python", sys.executable,
                ])
            self.assertEqual(rc, 0)
            data = json.loads((work / "blocks.json").read_text(encoding="utf-8"))
            block = data["pages"][0]["blocks"][0]
            self.assertEqual(block["layout_uid"], provenance.block_identity(1, block)["layout_uid"])


class AutoTranslateTests(unittest.TestCase):
    def test_partial_json_retries_only_missing_ids(self):
        calls = []

        def fake_call_api(cfg, messages, timeout, temperature):
            payload = messages[-1]["content"]
            calls.append(payload)
            if len(calls) == 1:
                return '{"a":"甲"}', {"total_tokens": 10}
            return '{"b":"乙"}', {"total_tokens": 5}

        args = SimpleNamespace(
            retry=0,
            split_depth=3,
            timeout=1.0,
            temperature=0.0,
            abort=threading.Event(),
        )
        with mock.patch.object(auto_translate, "call_api", side_effect=fake_call_api):
            out, usage = auto_translate.do_batch(
                {"base": "http://example", "model": "m", "key": "k"},
                [("a", "alpha"), ("b", "beta")], [], args)
        self.assertEqual(out, {"a": "甲", "b": "乙"})
        self.assertEqual(usage["total_tokens"], 15)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('"id": "a"', calls[1])
        self.assertIn('"id": "b"', calls[1])

    def test_resume_rejects_stale_or_unhashed_entries_when_hash_available(self):
        valid, stats = auto_translate.filter_resume_entries(
            {
                "p1b0": {"zh": "甲", "source_hash": "same", "layout_uid": "l0"},
                "p1b1": {"zh": "乙", "source_hash": "old", "layout_uid": "l1"},
                "p1b2": {"zh": "丙"},
                "gone": {"zh": "丁", "source_hash": "x"},
            },
            {
                "p1b0": {"source_hash": "same", "layout_uid": "l0"},
                "p1b1": {"source_hash": "new", "layout_uid": "l1"},
                "p1b2": {"source_hash": "h2", "layout_uid": "l2"},
            },
        )
        self.assertEqual(set(valid), {"p1b0"})
        self.assertEqual(stats["stale"], 1)
        self.assertEqual(stats["legacy_invalidated"], 1)
        self.assertEqual(stats["orphan"], 1)

    def test_v4_kinds_drive_skip_policy(self):
        ctx = {
            "force": set(), "landscape": set(), "front": set(),
            "refs_auto": False, "skip_pages": set(), "in_table": lambda b: False,
        }
        base = {"id": "x", "text": "content", "bbox": [20, 100, 200, 120]}
        for kind in ("table", "meta", "math_only"):
            block = {**base, "kind": kind}
            self.assertTrue(auto_translate.should_skip(block, 1, (0, 800), ctx)[0])

    def test_batch_context_is_not_added_to_output_ids(self):
        messages = auto_translate.build_messages(
            [("p2b0", "continued sentence")], [],
            context=("previous page ending", "next paragraph"),
        )
        self.assertIn("previous page ending", messages[0]["content"])
        self.assertNotIn("previous page ending", messages[1]["content"])

    def test_continuation_flags_are_sent_to_model(self):
        messages = auto_translate.build_messages(
            [("p2b0", "continued sentence")], [],
            item_meta={"p2b0": {"continues_from_prev": True}},
        )
        self.assertIn('"continues_from_prev": true', messages[1]["content"])

    def test_resume_gaps_start_new_batches(self):
        items = [("a", "alpha"), ("c", "charlie"), ("d", "delta")]
        batches = auto_translate.make_flow_batches(
            items, 1000, {"a": 0, "c": 2, "d": 3})
        self.assertEqual([[item[0] for item in batch] for batch in batches],
                         [["a"], ["c", "d"]])

    def test_structural_break_splits_adjacent_global_positions(self):
        items = [("a", "alpha"), ("heading", "Section"), ("b", "beta")]
        batches = auto_translate.make_flow_batches(
            items, 1000, {"a": 0, "heading": 1, "b": 2}, {"heading"})
        self.assertEqual([[item[0] for item in batch] for batch in batches],
                         [["a"], ["heading"], ["b"]])

    def test_dry_run_uses_skipped_blocks_in_global_flow_positions(self):
        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            blocks_path = td / "blocks.json"
            blocks = [
                {"id": "a", "text": "A sufficiently long body paragraph ends here.",
                 "kind": "body", "column": "left", "bbox": [40, 100, 260, 130],
                 "flow_index": 0},
                {"id": "table", "text": "table cell", "kind": "table",
                 "column": "full", "bbox": [40, 150, 560, 250], "flow_index": 1},
                {"id": "b", "text": "Another sufficiently long body paragraph starts here.",
                 "kind": "body", "column": "left", "bbox": [40, 280, 260, 310],
                 "flow_index": 2},
            ]
            blocks_path.write_text(json.dumps({
                "schema_version": 4, "page_count": 2,
                "pages": [{"page": 2, "width": 600, "height": 800,
                           "blocks": blocks}],
            }), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                rc = auto_translate.main([
                    "--blocks", str(blocks_path),
                    "--output", str(td / "translations.json"), "--dry-run",
                ])
            self.assertEqual(rc, 0)
            self.assertIn("-> 2 批", output.getvalue())


class ParagraphFlowTests(unittest.TestCase):
    def test_pdf_physical_lines_are_joined_inside_one_block(self):
        block = {"lines": [
            {"spans": [{"text": "A sentence ends here."}]},
            {"spans": [{"text": "The same paragraph continues."}]},
        ]}
        self.assertEqual(
            extract_blocks.block_text(block),
            "A sentence ends here. The same paragraph continues.",
        )

    def test_translation_hard_breaks_are_removed(self):
        self.assertEqual(
            auto_translate.normalize_translation_text("这是第一句。\n这是第二句，含 LLM 与 RL。"),
            "这是第一句。这是第二句，含LLM与RL。",
        )
        self.assertEqual(
            build_dual.normalize_translation_text("line one\nline two"),
            "line one line two",
        )

    def test_cross_page_continuation_is_never_indented(self):
        self.assertEqual(
            build_dual.block_indent_prefix(
                {"continues_from_prev": True}, "续句", "上一段。"),
            "",
        )

    def test_geometry_merges_sentence_boundary_fragments(self):
        a = {"bbox": [10, 10, 200, 30], "size": 10, "text": "Sentence one.",
             "heading": False, "bold": False, "math_only": False}
        b = {"bbox": [10, 34, 200, 54], "size": 10, "text": "Sentence two starts here.",
             "heading": False, "bold": False, "math_only": False}
        self.assertTrue(merge_paragraphs.continuable(a, b, 0.6))

    def test_geometry_does_not_merge_list_or_reference_start(self):
        a = {"bbox": [10, 10, 200, 30], "size": 10, "text": "Sentence one.",
             "heading": False, "bold": False, "math_only": False}
        b = {"bbox": [10, 34, 200, 54], "size": 10, "text": "[12] Smith et al.",
             "heading": False, "bold": False, "math_only": False}
        self.assertFalse(merge_paragraphs.continuable(a, b, 0.6))


class ProvenanceTests(unittest.TestCase):
    def test_block_hash_is_whitespace_stable(self):
        self.assertEqual(
            provenance.block_source_hash(3, "alpha   beta\n gamma"),
            provenance.block_source_hash(3, "alpha beta gamma"),
        )

    def test_layout_uid_distinguishes_same_text_at_different_positions(self):
        left = provenance.block_layout_uid(1, "left", [10, 20, 200, 40], "same")
        right = provenance.block_layout_uid(1, "right", [310, 20, 500, 40], "same")
        self.assertNotEqual(left, right)

    def test_refresh_identity_tracks_final_bbox(self):
        block = {"id": "p1b0", "text": "same", "column": "left",
                 "bbox": [10, 20, 200, 40]}
        data = {"pages": [{"page": 1, "blocks": [block]}]}
        provenance.refresh_block_identities(data)
        before = block["layout_uid"]
        block["bbox"][2] = 240
        self.assertEqual(provenance.refresh_block_identities(data), 1)
        self.assertNotEqual(block["layout_uid"], before)

    def test_manifest_detects_replaced_source(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "paper.pdf"
            src.write_bytes(b"first")
            manifest = td / "manifest.json"
            provenance.write_manifest(manifest, src, src, "all")
            ok, _, _ = provenance.validate_manifest(manifest, src)
            self.assertTrue(ok)
            src.write_bytes(b"second")
            ok, _, _ = provenance.validate_manifest(manifest, src)
            self.assertFalse(ok)

    def test_prepared_pages_must_cover_build_pages(self):
        self.assertTrue(provenance.page_selection_covers("all", "2-4"))
        self.assertTrue(provenance.page_selection_covers("2-5", "3,5"))
        self.assertFalse(provenance.page_selection_covers("2-4", "all"))
        self.assertFalse(provenance.page_selection_covers("2-4", "1-3"))


class TranslationHashTests(unittest.TestCase):
    def test_build_rejects_mismatched_translation_hash(self):
        block = {"id": "p1b0", "text": "new", "column": "left",
                 "bbox": [10, 20, 200, 40]}
        identity = provenance.block_identity(1, block)
        blocks = {"pages": [{"page": 1, "blocks": [block]}]}
        mismatch, unverified = build_dual.validate_translation_hashes(
            blocks, {"p1b0": {"zh": "译文", "source_hash": "old",
                               "layout_uid": identity["layout_uid"]}})
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(unverified, 0)

    def test_v4_missing_layout_uid_is_unverified(self):
        block = {"id": "p1b0", "text": "source", "column": "left",
                 "bbox": [10, 20, 200, 40]}
        identity = provenance.block_identity(1, block)
        blocks = {"schema_version": 4, "pages": [{"page": 1, "blocks": [block]}]}
        mismatch, unverified = build_dual.validate_translation_identities(
            blocks, {"p1b0": {"zh": "译文", "source_hash": identity["source_hash"]}})
        self.assertEqual(mismatch, [])
        self.assertEqual(unverified, 1)

    def test_same_text_swap_is_rejected_by_layout_uid(self):
        block = {"id": "p1b0", "text": "same", "column": "left",
                 "bbox": [10, 20, 200, 40]}
        identity = provenance.block_identity(1, block)
        blocks = {"schema_version": 4, "pages": [{"page": 1, "blocks": [block]}]}
        mismatch, unverified = build_dual.validate_translation_identities(
            blocks, {"p1b0": {"zh": "译文", "source_hash": identity["source_hash"],
                               "layout_uid": "right"}})
        self.assertEqual(mismatch[0][1], "layout_uid")
        self.assertEqual(unverified, 0)

    def test_build_recomputes_layout_uid_instead_of_trusting_stored_value(self):
        block = {"id": "p1b0", "text": "same", "column": "left",
                 "bbox": [10, 20, 200, 40]}
        old = provenance.block_identity(1, block)
        block.update(old)
        block["bbox"] = [10, 20, 260, 40]
        blocks = {"schema_version": 4, "pages": [{"page": 1, "blocks": [block]}]}
        mismatch, unverified = build_dual.validate_translation_identities(
            blocks, {"p1b0": {"zh": "译文", **old}})
        self.assertEqual(mismatch[0][1], "layout_uid")
        self.assertEqual(unverified, 0)


class TableExtractionTests(unittest.TestCase):
    def test_caption_stops_before_rule_inside_same_text_block(self):
        def line(text, y0, y1, x0=40, x1=300, size=10):
            return {"bbox": [x0, y0, x1, y1], "spans": [
                {"text": text, "bbox": [x0, y0, x1, y1], "size": size}]}

        block = {"type": 0, "lines": [
            line("Table 1. Applications of", 90, 100),
            line("language models.", 102, 112),
            line("Method                         Score", 125, 135),
        ]}
        page = SimpleNamespace(
            rect=SimpleNamespace(width=600),
            get_text=lambda mode: {"blocks": [block]},
        )
        captions = extract_tables.caption_lines(page, [(120, 40, 560)])
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0]["text"],
                         "Table 1. Applications of language models.")
        self.assertLess(captions[0]["bbox"][3], 120)

    def test_distant_rule_cluster_is_not_attached_to_caption(self):
        caps = [{"text": "Table 1", "bbox": [40, 90, 200, 110],
                 "y": 90, "y1": 110, "x0": 40, "x1": 200, "xc": 120}]
        rules = [(180, 40, 560), (220, 40, 560), (260, 40, 560),
                 (500, 40, 560), (530, 40, 560)]
        groups = extract_tables.group_by_caption(rules, caps, 300)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["y1"], 260)


if __name__ == "__main__":
    unittest.main()
