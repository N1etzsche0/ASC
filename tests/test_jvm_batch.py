"""Bounded CFR grouping, failure isolation, shared context and real inner exports."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from droidasc.jvm.application import JvmApplication, entry_container
from droidasc.jvm.classfile import ClassFile
from droidasc.jvm.decompiler import BatchTimeout, decompile_batch
from droidasc.jvm.exporter import export_sources


@unittest.skipUnless(shutil.which("javac"), "JDK required for batch fixtures")
class JvmBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        source = cls.root / "source"
        source.mkdir()
        code = {
            "Helper.java": 'package batch; public class Helper { public static int value(){return 77;} }',
            "One.java": 'package batch; public class One { public int value(){return Helper.value();} public static class Inner { public int value(){return 17;} } }',
            "Two.java": 'package batch; public class Two { public int value(){return Helper.value()+1;} }',
        }
        for name, text in code.items():
            (source / name).write_text(text)
        cls.classes = cls.root / "classes"
        subprocess.run([shutil.which("javac"), "--release", "8", "-d", str(cls.classes),
                        *map(str, sorted(source.glob("*.java")))], check=True, capture_output=True, timeout=60)
        cls.jar = cls.root / "sample.jar"
        with zipfile.ZipFile(cls.jar, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(cls.classes.rglob("*.class")):
                archive.write(path, path.relative_to(cls.classes).as_posix())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        # Grouping tests must behave identically on small CI runners and big hosts.
        probe = patch("droidasc.jvm.memory.available_memory_bytes", return_value=8 * 1024 ** 3)
        probe.start()
        self.addCleanup(probe.stop)

    @staticmethod
    def fake_batch(application, entries, *args, class_data=None, **kwargs):
        entries = list(entries)
        assert len({entry_container(entry) for entry in entries}) == 1
        assert len({entry.name for entry in entries}) == len(entries)
        for entry in entries:
            assert ClassFile(class_data[entry.origin]).name == entry.name
            yield entry, "// " + entry.origin + "\nclass " + entry.name.split("/")[-1] + " {}"

    def test_grouped_export_resume_and_edited_output_only_retries_one_class(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            output = Path(temp) / "out"
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=self.fake_batch) as batch, \
                    patch("droidasc.jvm.exporter.decompile", return_value="class replacement {}") as single:
                first = list(export_sources(app, output, resources=False))
                self.assertEqual(batch.call_count, 1)
                self.assertEqual(single.call_count, 0)
                self.assertEqual(len(first), 4)
                second = list(export_sources(app, output, resources=False, resume=True))
                self.assertTrue(all(record["resumed"] for record in second))
                self.assertEqual(batch.call_count, 1)
                (output / first[0]["output"]).write_text("edited")
                third = list(export_sources(app, output, resources=False, resume=True))
                self.assertEqual(single.call_count, 1)
                self.assertEqual(sum(not record["resumed"] for record in third), 1)
                self.assertTrue(all(record["status"] == "ok" for record in third))

    def test_nested_and_duplicate_containers_are_never_combined(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            war = temp / "sample.war"
            with zipfile.ZipFile(war, "w") as archive:
                archive.writestr("WEB-INF/lib/first.jar", self.jar.read_bytes())
                archive.writestr("WEB-INF/lib/second.jar", self.jar.read_bytes())
            with JvmApplication(war) as app, patch("droidasc.jvm.exporter.decompile_batch", side_effect=self.fake_batch) as batch:
                records = list(export_sources(app, temp / "out", resources=False))
            self.assertEqual(batch.call_count, 2)
            self.assertEqual(len(records), 8)
            self.assertEqual(len({record["output"] for record in records}), 8)
            self.assertTrue(all(record["status"] == "ok" for record in records))

    def test_batch_size_selection_and_fingerprint_changes(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            output = Path(temp) / "out"
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=self.fake_batch) as batch:
                first = list(export_sources(app, output, resources=False, batch_size=2))
                self.assertEqual(batch.call_count, 2)
                second = list(export_sources(app, output, resources=False, batch_size=64, resume=True))
                self.assertEqual(batch.call_count, 3)
                self.assertTrue(all(not record["resumed"] for record in second))
                self.assertNotEqual(first[0]["config_hash"], second[0]["config_hash"])
            with patch("droidasc.jvm.exporter.decompile", return_value="class chosen {}") as single, \
                    patch("droidasc.jvm.exporter.decompile_batch") as batch:
                chosen = list(export_sources(app, Path(temp) / "selected", resources=False,
                                             prefix="batch.One", limit=1))
            self.assertEqual(len(chosen), 1)
            self.assertEqual(single.call_count, 1)
            batch.assert_not_called()

    def test_auto_memory_change_keeps_completed_sources_and_changes_remaining_groups(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            output = Path(temp) / "out"
            stats = {}
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=self.fake_batch) as batch, \
                    patch("droidasc.jvm.exporter.decompile", return_value="class recovered {}") as single:
                first = list(export_sources(app, output, resources=False, stats=stats))
                self.assertEqual(stats["batch_size"], 64)
                (output / first[0]["output"]).write_text("edited")
                with patch("droidasc.jvm.memory.available_memory_bytes", return_value=512 * 1024 ** 2):
                    second = list(export_sources(app, output, resources=False, resume=True, stats=stats))
                self.assertEqual(stats["batch_size"], 1)
                self.assertEqual(stats["batch_size_mode"], "auto")
                self.assertEqual(sum(record["resumed"] for record in second), 3)
                self.assertEqual(first[0]["config_hash"], second[0]["config_hash"])
                self.assertEqual(batch.call_count, 1)
                self.assertEqual(single.call_count, 1)
                repaired = next(record for record in second if not record["resumed"])
                self.assertEqual(repaired["batch_size"], 1)

    def test_payload_limit_splits_batches(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            sizes = sorted(len(app.read(entry)) for entry in app.entries)
            limit = max(sizes) + min(sizes)

            def batch(application, entries, *args, class_data=None, **kwargs):
                self.assertLessEqual(sum(len(data) for data in class_data.values()), limit)
                yield from self.fake_batch(application, entries, *args, class_data=class_data, **kwargs)

            with patch("droidasc.jvm.exporter.MAX_BATCH_BYTES", limit), \
                    patch("droidasc.jvm.exporter.decompile_batch", side_effect=batch) as grouped, \
                    patch("droidasc.jvm.exporter.decompile", return_value="class selected {}") as single:
                records = list(export_sources(app, Path(temp) / "out", resources=False))
            self.assertEqual(len(records), 4)
            self.assertTrue(all(record["status"] == "ok" for record in records))
            self.assertGreater(grouped.call_count + single.call_count, 1)

    def test_partial_batch_retries_only_missing_source_and_persists_every_record(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            def partial(application, entries, *args, **kwargs):
                for index, entry in enumerate(entries):
                    yield entry, ValueError("missing source") if index == 1 else "class output {}"

            output = Path(temp) / "out"
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=partial), \
                    patch("droidasc.jvm.exporter.decompile", return_value="class recovered {}") as retry:
                records = list(export_sources(app, output, resources=False))
            self.assertEqual(retry.call_count, 1)
            self.assertTrue(all(record["status"] == "ok" for record in records))
            saved = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
            self.assertEqual({record["origin"] for record in saved}, {record["origin"] for record in records})

    def test_bad_shared_context_isolates_good_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            bad = temp / "bad.jar"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr("batch/Two.class", (self.classes / "batch/Two.class").read_bytes())
                archive.writestr("batch/Invalid.class", b"invalid")
            with JvmApplication(bad, tolerant=True) as app:
                def single(application, entry, *args):
                    ClassFile(application.read(entry))
                    return "class Two {}"
                with patch("droidasc.jvm.exporter.decompile_batch", side_effect=ValueError("context failure")), \
                        patch("droidasc.jvm.exporter.decompile", side_effect=single) as retry:
                    records = list(export_sources(app, temp / "out", resources=False))
            self.assertEqual(retry.call_count, 2)
            self.assertEqual(sum(record["status"] == "ok" for record in records), 1)
            self.assertEqual(sum(record["status"] == "error" for record in records), 1)

    def test_batch_timeout_does_not_multiply_retries(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=BatchTimeout("group timeout")), \
                    patch("droidasc.jvm.exporter.decompile") as single:
                records = list(export_sources(app, Path(temp) / "out", resources=False))
            single.assert_not_called()
            self.assertEqual(len(records), 4)
            self.assertTrue(all(record["status"] == "error" and "group timeout" in record["error"] for record in records))

    def test_class_reader_reuses_zip_and_shared_context_keeps_required_roots(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            real_zip = zipfile.ZipFile
            with patch("droidasc.jvm.application.zipfile.ZipFile", wraps=real_zip) as opened:
                with app.class_reader() as read:
                    for entry in app.entries:
                        self.assertEqual(ClassFile(read(entry)).name, entry.name)
                    read(app.entries[0])
                self.assertEqual(opened.call_count, 1)
            roots = [app.select("batch.One"), app.select("batch.Two")]
            data = {entry.origin: app.read(entry) for entry in roots}
            destination = Path(temp) / "context"
            with patch("droidasc.jvm.application.zipfile.ZipFile", wraps=real_zip) as opened:
                self.assertFalse(app.stage_contexts(roots, destination, class_data=data))
                self.assertEqual(opened.call_count, 1)
            self.assertEqual(len(list(destination.rglob("*.class"))), 4)
            with self.assertRaisesRegex(ValueError, "Selected classes"):
                app.stage_contexts(roots, Path(temp) / "small", max_classes=1)

    def test_cfr_batch_missing_files_are_individual_results(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            backend = Path(temp) / "fake.jar"
            backend.write_bytes(b"backend")
            entries = [app.select("batch.One"), app.select("batch.Two")]

            def run(command, **kwargs):
                output = Path(command[command.index("--outputpath") + 1])
                (output / "batch").mkdir(parents=True)
                (output / "batch/One.java").write_text("class One {}")
                self.assertIn("--skipbatchinnerclasses", command)
                self.assertEqual(sum(arg.endswith(".class") for arg in command), 2)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("droidasc.jvm.decompiler.subprocess.run", side_effect=run) as invoked:
                results = list(decompile_batch(app, entries, backend))
            self.assertEqual(invoked.call_count, 1)
            self.assertIsInstance(results[0][1], str)
            self.assertIsInstance(results[1][1], ValueError)

    def test_invalid_batch_options_fail_before_backend(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            for size in (0, 257):
                with self.assertRaisesRegex(ValueError, "batch size"):
                    list(export_sources(app, Path(temp) / "out", batch_size=size))
            with self.assertRaisesRegex(ValueError, "timeout"):
                list(export_sources(app, Path(temp) / "out", timeout=0))
            for heap in (0, 32, 8193, 128.5):
                with self.assertRaisesRegex(ValueError, "heap"):
                    list(export_sources(app, Path(temp) / "out", heap_mb=heap))

    def test_custom_heap_reaches_batch_and_retry_and_invalidates_resume(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            output = Path(temp) / "out"
            stats = {}
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=self.fake_batch) as batch:
                first = list(export_sources(app, output, resources=False))
                second = list(export_sources(app, output, resources=False, resume=True, heap_mb=2048, stats=stats))
                self.assertEqual(batch.call_count, 2)
                self.assertEqual(batch.call_args.kwargs["heap_mb"], 2048)
                self.assertEqual(stats["heap_mb"], 2048)
                self.assertTrue(all(not record["resumed"] for record in second))
                self.assertNotEqual(first[0]["config_hash"], second[0]["config_hash"])
            with patch("droidasc.jvm.exporter.decompile", return_value="class chosen {}") as single:
                list(export_sources(app, Path(temp) / "single", resources=False, limit=1, heap_mb=2048))
                self.assertEqual(single.call_args.kwargs["heap_mb"], 2048)

    def test_enum_switch_profile_reaches_backends_and_invalidates_resume(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jar) as app:
            output = Path(temp) / "out"
            stats = {}
            with patch("droidasc.jvm.exporter.decompile_batch", side_effect=self.fake_batch) as batch:
                first = list(export_sources(app, output, resources=False))
                second = list(export_sources(app, output, resources=False, resume=True,
                                             decode_enum_switch=False, stats=stats))
                self.assertEqual(batch.call_args.kwargs["decode_enum_switch"], False)
                self.assertFalse(stats["decode_enum_switch"])
                self.assertTrue(all(not record["resumed"] for record in second))
                self.assertNotEqual(first[0]["config_hash"], second[0]["config_hash"])
            with patch("droidasc.jvm.exporter.decompile", return_value="class chosen {}") as single:
                list(export_sources(app, Path(temp) / "single", resources=False, limit=1, decode_enum_switch=False))
                self.assertFalse(single.call_args.kwargs["decode_enum_switch"])

    @unittest.skipUnless(os.environ.get("ASC_TEST_CFR_JAR"), "Set ASC_TEST_CFR_JAR for real CFR batch integration")
    def test_real_cfr_batch_preserves_inner_classes_and_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            output = temp / "out"
            command = [sys.executable, str(ROOT / "main.py"), "export", str(self.jar),
                       "-o", str(output), "--no-resources", "--prefix", "batch.One",
                       "--cfr-jar", os.environ["ASC_TEST_CFR_JAR"], "--batch-size", "64", "--heap-mb", "1024",
                       "--no-enum-switch"]
            first = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(first.returncode, 0, first.stderr)
            summary = json.loads(first.stderr.splitlines()[-1])
            self.assertEqual(summary["batch_size"], 64)
            self.assertEqual(summary["batch_size_mode"], "explicit")
            self.assertEqual(summary["heap_mb"], 1024)
            self.assertFalse(summary["decode_enum_switch"])
            records = [json.loads(line) for line in first.stdout.splitlines()]
            self.assertEqual({record["class"] for record in records}, {"Lbatch/One;", "Lbatch/One$Inner;"})
            self.assertTrue(all(record["status"] == "ok" for record in records))
            self.assertEqual(len(list(output.rglob("*.java"))), 2)
            outer = next(record for record in records if record["class"] == "Lbatch/One;")
            self.assertIn("Helper.value()", (output / outer["output"]).read_text())
            second = subprocess.run(command + ["--resume"], capture_output=True, text=True, timeout=60)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(all(json.loads(line)["resumed"] for line in second.stdout.splitlines()))

    @unittest.skipUnless(os.environ.get("ASC_TEST_CFR_JAR"), "Set ASC_TEST_CFR_JAR for real nested CFR integration")
    def test_real_cfr_nested_anonymous_batch_export(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = temp / "Generated.java"
            source.write_text('package batch; public class Generated { public Runnable create(){ '
                              'return new Runnable(){ public void run(){ System.out.print("anonymous"); } }; } }')
            classes = temp / "classes"
            subprocess.run([shutil.which("javac"), "--release", "8", "-d", str(classes), str(source)],
                           check=True, capture_output=True, timeout=60)
            library = temp / "generated.jar"
            with zipfile.ZipFile(library, "w") as archive:
                for path in sorted(classes.rglob("*.class")):
                    archive.write(path, path.relative_to(classes).as_posix())
            war = temp / "generated.war"
            with zipfile.ZipFile(war, "w") as archive:
                archive.writestr("WEB-INF/lib/generated.jar", library.read_bytes())
            output = temp / "out"
            command = [sys.executable, str(ROOT / "main.py"), "export", str(war), "-o", str(output),
                       "--no-resources", "--cfr-jar", os.environ["ASC_TEST_CFR_JAR"]]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stderr.splitlines()[-1])
            self.assertEqual(summary["batch_size_mode"], "auto")
            self.assertIn(summary["batch_size"], {1, 8, 16, 32, 64})
            self.assertIn("available_memory_bytes", summary)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual({r["class"] for r in records}, {"Lbatch/Generated;", "Lbatch/Generated$1;"})
            self.assertTrue(all(r["status"] == "ok" and "!/WEB-INF/lib/generated.jar!/" in r["origin"] for r in records))
            self.assertEqual(len(list(output.rglob("*.java"))), 2)


if __name__ == "__main__":
    unittest.main()
