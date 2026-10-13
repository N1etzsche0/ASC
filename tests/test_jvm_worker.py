"""Persistent CFR output identity, context isolation and process cleanup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from droidasc.jvm.application import JvmApplication
from droidasc.jvm.decompiler import HeapFailure, _failure
from droidasc.jvm.context import ContextMetadataCache
from droidasc.jvm.exporter import export_sources
from droidasc.jvm.worker import CfrWorkerPool


@unittest.skipUnless(shutil.which("javac"), "JDK required for CFR worker fixtures")
class JvmWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.java = shutil.which("java")
        source = cls.root / "Main.java"
        source.write_text('''package org.benf.cfr.reader;
public class Main {
    public static void main(String[] args) throws Exception {
        if (args[0].equals("stall")) Thread.sleep(60000);
        if (args[0].equals("crash")) Runtime.getRuntime().halt(7);
        if (args[0].equals("heap")) {
            System.err.println("java.lang.OutOfMemoryError: Java heap space");
            for (int i=0; i<140000; i++) System.err.print("x");
        }
        System.out.println("diagnostic output is not the protocol");
    }
}''')
        classes = cls.root / "fake-classes"
        subprocess.run(["javac", "--release", "8", "-d", str(classes), str(source)],
                       check=True, capture_output=True, timeout=60)
        cls.fake = cls.root / "fake-cfr.jar"
        with zipfile.ZipFile(cls.fake, "w") as archive:
            for path in classes.rglob("*.class"):
                archive.write(path, path.relative_to(classes))
        cls.jars = []
        for value in (17, 71):
            source.write_text(f'''package repeated;
public class Main {{
 public int value() {{ return {value}; }}
 public static class Inner {{ public String text() {{ return "雪{value}"; }} }}
 public Runnable task() {{ return new Runnable() {{ public void run() {{ System.out.println({value}); }} }}; }}
}}''')
            classes = cls.root / f"classes-{value}"
            subprocess.run(["javac", "--release", "8", "-d", str(classes), str(source)],
                           check=True, capture_output=True, timeout=60)
            jar = cls.root / f"sample-{value}.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                for path in sorted(classes.rglob("*.class")):
                    archive.write(path, path.relative_to(classes))
            cls.jars.append(jar)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def command(self, case):
        return [self.java, "-Xmx128m", "-jar", str(self.fake), case]

    def test_worker_reuses_process_and_bounds_diagnostics_without_losing_heap_failure(self):
        pool = CfrWorkerPool("worker")
        try:
            worker = pool.worker()
            first = worker.run(self.command("ok"), 10)
            process = worker.process
            self.assertEqual(first.returncode, 0)
            self.assertIn("diagnostic output", first.stderr)
            self.assertEqual(worker.run(self.command("ok"), 10).returncode, 0)
            self.assertIs(worker.process, process)
            heap = worker.run(self.command("heap"), 10)
            self.assertIsInstance(_failure(heap, "failed"), HeapFailure)
            self.assertLess(len(heap.stderr), 66000)
            self.assertIsNone(worker.process)
            self.assertIsNotNone(process.poll())
            self.assertEqual(worker.run(self.command("ok"), 10).returncode, 0)
            self.assertEqual(pool.counters["cfr_worker_starts"], 2)
        finally:
            pool.close()

    def test_worker_timeout_and_crash_reap_process_and_allow_next_request(self):
        pool = CfrWorkerPool("worker")
        try:
            worker = pool.worker()
            self.assertEqual(worker.run(self.command("ok"), 10).returncode, 0)
            process = worker.process
            with self.assertRaises(subprocess.TimeoutExpired):
                worker.run(self.command("stall"), 0.1)
            self.assertIsNone(worker.process)
            self.assertIsNotNone(process.poll())
            failure = worker.run(self.command("crash"), 10)
            self.assertEqual(failure.returncode, 1)
            self.assertIsNone(worker.process)
            self.assertEqual(worker.run(self.command("ok"), 10).returncode, 0)
        finally:
            pool.close()

    def test_auto_compiler_fallback_is_once_and_explicit_worker_fails(self):
        # A selected JRE path has no adjacent compiler; no compiler is on PATH.
        missing_java = str(self.root / "jre/bin/java")
        with patch("droidasc.jvm.worker.shutil.which", return_value=None):
            pool = CfrWorkerPool("auto")
            try:
                with self.assertWarnsRegex(RuntimeWarning, "using process mode"):
                    self.assertIsNone(pool.bridge(missing_java))
                self.assertIsNone(pool.bridge(missing_java))
                self.assertEqual(pool.counters["cfr_worker_fallbacks"], 1)
            finally:
                pool.close()
            pool = CfrWorkerPool("worker")
            try:
                with self.assertRaisesRegex(ValueError, "requires javac"):
                    pool.bridge(missing_java)
                with self.assertRaisesRegex(ValueError, "requires javac"):
                    pool.bridge(missing_java)
            finally:
                pool.close()

    def test_auto_pool_reserves_heap_headroom_and_avoids_small_export_startup_cost(self):
        def source(application, entry, *args, **kwargs):
            return "class cached {}"

        for memory, batch, expected in ((8 * 1024 ** 3, 64, "small-export"),
                                        (512 * 1024 ** 2, 1, "memory"),
                                        (8 * 1024 ** 3, 1, "reuse")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp, \
                    JvmApplication(self.jars[0]) as app, \
                    patch("droidasc.jvm.exporter.available_memory_bytes", return_value=memory), \
                    patch("droidasc.jvm.exporter.decompile", side_effect=source), \
                    patch("droidasc.jvm.exporter.decompile_batch", side_effect=lambda a, es, *args, **kw: ((e, "class cached {}") for e in es)):
                stats = {}
                list(export_sources(app, Path(temp) / "out", workers=1, resources=False,
                                    batch_size=batch, stats=stats))
                self.assertEqual(stats["cfr_worker_policy"], expected)

    def test_small_auto_and_process_mode_do_not_compile_bridge(self):
        for mode, small in (("auto", True), ("process", False)):
            pool = CfrWorkerPool(mode, small_export=small)
            try:
                with patch("droidasc.jvm.worker.subprocess.run") as compiler:
                    self.assertIsNone(pool.bridge(self.java))
                compiler.assert_not_called()
            finally:
                pool.close()

    def test_context_cache_validates_current_bytes_names_and_memory_bounds(self):
        cache = ContextMetadataCache(max_entries=1)
        with JvmApplication(self.jars) as app:
            entries = [entry for entry in app.classes() if entry.name == "repeated/Main"]
            data = [app.read(entry) for entry in entries]
            dependencies = cache.dependencies(entries[0], data[0])
            self.assertEqual(cache.dependencies(entries[0], data[0]), dependencies)
            self.assertEqual((cache.hits, cache.misses), (1, 1))
            # A changed definition of the same name must be parsed again.
            cache.dependencies(entries[1], data[1])
            self.assertEqual(cache.misses, 2)
            self.assertEqual(len(cache.entries), 1)
            cache.dependencies(entries[0], data[0])
            self.assertEqual(cache.misses, 3)
            wrong = SimpleNamespace(name="wrong/Main", origin="wrong.class")
            with self.assertRaisesRegex(ValueError, "entry/name mismatch"):
                cache.dependencies(wrong, data[0])
            with self.assertRaises(ValueError):
                cache.dependencies(entries[0], b"corrupt current bytes")
            tiny = ContextMetadataCache(max_bytes=1)
            self.assertEqual(tiny.dependencies(entries[0], data[0]), dependencies)
            self.assertEqual(tiny.size, 0)
            self.assertFalse(tiny.entries)

    def test_context_cache_preserves_staged_dependency_closure(self):
        cache = ContextMetadataCache()
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jars[0]) as app:
            entry = app.select("repeated.Main")
            paths = [Path(temp) / name for name in ("uncached", "cached", "reused")]
            for path, chosen in zip(paths, (None, cache, cache)):
                self.assertFalse(app.stage_context(entry, path, metadata_cache=chosen))
            snapshots = [{str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*.class")}
                         for path in paths]
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(snapshots[1], snapshots[2])
            self.assertEqual(cache.misses, 3)
            self.assertEqual(cache.hits, 3)

    def test_archive_byte_cache_is_bounded_and_loose_classes_remain_fresh(self):
        from droidasc.jvm.application import _read_class
        with JvmApplication(self.jars[0]) as app:
            entry = app.select("repeated.Main")
            for budget, expected in ((16 * 1024 * 1024, 1), (1, 2)):
                with patch("droidasc.jvm.application._read_class", wraps=_read_class) as read_class, \
                        app.class_reader(byte_cache_bytes=budget) as read:
                    self.assertEqual(read(entry), read(entry))
                    self.assertEqual(read_class.call_count, expected)
            with app.class_reader(byte_cache_bytes=16 * 1024 * 1024, byte_cache_entries=1) as read, \
                    patch("droidasc.jvm.application._read_class", wraps=_read_class) as read_class:
                read(entry)
                read(app.select("repeated.Main$Inner"))
                read(entry)
                self.assertEqual(read_class.call_count, 3)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Main.class"
            with zipfile.ZipFile(self.jars[0]) as archive:
                path.write_bytes(archive.read("repeated/Main.class"))
            with JvmApplication(path) as app, app.class_reader() as read:
                entry = app.select("repeated.Main")
                first = read(entry)
                with zipfile.ZipFile(self.jars[1]) as archive:
                    path.write_bytes(archive.read("repeated/Main.class"))
                self.assertNotEqual(read(entry), first)

    def test_exclusive_heap_retry_closes_idle_workers_before_starting_larger_jvm(self):
        processes = []

        def backend(application, entry, *args, heap_mb=128, worker=None, **kwargs):
            if worker is not None:
                self.assertEqual(worker.run(self.command("ok"), 10).returncode, 0)
                processes.append(worker.process)
                raise HeapFailure("Java heap space")
            self.assertEqual(heap_mb, 256)
            self.assertTrue(all(process.poll() is not None for process in processes))
            return "class recovered {}"

        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jars[0]) as app, \
                patch("droidasc.jvm.exporter.decompile", side_effect=backend):
            records = list(export_sources(app, Path(temp) / "out", workers=1, resources=False,
                                          limit=1, heap_mb=128, oom_heap_mb=256, cfr_mode="worker"))
            self.assertEqual(records[0]["status"], "ok")
            self.assertEqual(records[0]["oom_retries"], 1)
        self.assertTrue(all(process.poll() is not None for process in processes))

    @unittest.skipUnless(os.environ.get("ASC_TEST_CFR_JAR"), "Set ASC_TEST_CFR_JAR for real CFR")
    def test_real_cfr_worker_matches_process_across_duplicate_nested_containers_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            war = temp / "app.war"
            with zipfile.ZipFile(war, "w") as archive:
                for i, jar in enumerate(self.jars):
                    archive.writestr(f"WEB-INF/lib/{i}.jar", jar.read_bytes())
            cfr = os.environ["ASC_TEST_CFR_JAR"]
            hashes = {}
            with JvmApplication(war) as app:
                for mode in ("process", "worker"):
                    stats = {}
                    output = temp / mode
                    records = list(export_sources(app, output, cfr, workers=1,
                                                  resources=False, batch_size=2,
                                                  cfr_mode=mode, stats=stats))
                    self.assertEqual(len(records), 6)
                    self.assertTrue(all(r["status"] == "ok" for r in records), records)
                    hashes[mode] = {r["origin"]: r["output_sha256"] for r in records}
                    self.assertEqual(len(list(output.rglob("*.java"))), 6)
                    if mode == "worker":
                        self.assertEqual(stats["cfr_worker_starts"], 1)
                        self.assertEqual(stats["cfr_worker_requests"], 4)
                    # Execution mode does not change the source fingerprint.
                    resumed_stats = {}
                    resumed = list(export_sources(app, output, cfr, workers=1,
                                                  resources=False, batch_size=2, resume=True,
                                                  cfr_mode="worker", stats=resumed_stats))
                    self.assertTrue(all(r["resumed"] for r in resumed))
                    self.assertEqual(resumed_stats["cfr_worker_starts"], 0)
            self.assertEqual(hashes["process"], hashes["worker"])

    @unittest.skipUnless(os.environ.get("ASC_TEST_CFR_JAR"), "Set ASC_TEST_CFR_JAR for real CFR")
    def test_real_cfr_worker_generator_close_reaps_all_jvms(self):
        real_popen = subprocess.Popen
        processes = []

        def launch(command, **kwargs):
            child = real_popen(command, **kwargs)
            if command[-1] == "CfrWorker":
                processes.append(child)
            return child

        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.jars[0]) as app, \
                patch("droidasc.jvm.worker.subprocess.Popen", side_effect=launch):
            iterator = export_sources(app, Path(temp) / "out", os.environ["ASC_TEST_CFR_JAR"],
                                      workers=1, resources=False, batch_size=1, cfr_mode="worker")
            self.assertEqual(next(iterator)["status"], "ok")
            iterator.close()
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))


if __name__ == "__main__":
    unittest.main()
