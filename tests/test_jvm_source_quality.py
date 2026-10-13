import unittest
from pathlib import Path
import tempfile
import json
import hashlib

from jvm_source_quality import directory_quality, source_quality
from benchmark_jvm_large import inspect_outputs


class SourceQualityTests(unittest.TestCase):
    def test_jadx_warnings_are_not_counted_as_failed_methods(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Warnings.java").write_text("/* JADX WARN: restructure */\n/* JADX WARN: types */")
            (root / "Errors.java").write_text("/* JADX ERROR: exception */\nMethod not decompiled")
            report = directory_quality(root)
            self.assertEqual(report["jadx_warning_markers"], 2)
            self.assertEqual(report["jadx_error_markers"], 1)
            self.assertEqual(report["files_with_jadx_warnings"], 1)
            self.assertEqual(report["files_with_jadx_errors"], 1)
            self.assertEqual(report["failed_method_markers"], 1)

    def test_cfr_failed_method_and_structuring_diagnostics_are_separate(self):
        result = source_quality("/* This method has failed to decompile. */\n"
                                "/* Unable to fully structure code */")
        self.assertEqual(result["failed_method_markers"], 1)
        self.assertEqual(result["structuring_markers"], 1)
        self.assertEqual(source_quality("class Healthy {} ")["failed_method_markers"], 0)

    def test_jadx_failure_and_directory_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Failed.java").write_text("Method not decompiled\nThis method has failed to decompile.")
            (root / "Structured.java").write_text("Unable to fully structure code\nUnable to fully structure code")
            (root / "Healthy.java").write_text("class Healthy {}")
            report = directory_quality(root)
            self.assertEqual(report["java_files"], 3)
            self.assertEqual(report["files_with_failure_markers"], 1)
            self.assertEqual(report["failed_method_markers"], 2)
            self.assertEqual(report["files_with_structuring_markers"], 1)
            self.assertEqual(report["structuring_markers"], 2)

    def test_large_manifest_inspection_uses_latest_record_per_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = root / "export"
            export.mkdir()
            body = b"class Healthy {}"
            (export / "Healthy.java").write_bytes(body)
            records = [{"origin": "public.jar!/Healthy.class", "status": "error", "error": "old failure"},
                       {"origin": "public.jar!/Healthy.class", "status": "ok", "output": "Healthy.java",
                        "resumed": True, "output_sha256": hashlib.sha256(body).hexdigest()}]
            (export / "manifest.jsonl").write_text("\n".join(json.dumps(record) for record in records)+"\n")
            report = inspect_outputs(root)
            self.assertEqual(report["manifest_statuses"], {"ok": 1})
            self.assertEqual(report["unique_source_files"], 1)
            self.assertEqual(report["resumed_records"], 1)
            self.assertEqual(report["hash_mismatches"], 0)


if __name__ == "__main__":
    unittest.main()
