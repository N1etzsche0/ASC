"""Self-contained JVM integration fixtures, compiled locally without app dependencies."""
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from droidasc.jvm.application import JvmApplication, is_jvm_input
from droidasc.jvm.classfile import ClassFile, ClassFormatError, decode_mutf8, instructions
from droidasc.jvm.decompiler import decompile


ROOT = Path(__file__).resolve().parents[1]


def archive_bytes(entries):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


class ClassReaderTests(unittest.TestCase):
    def test_modified_utf8_null_and_surrogate_pair(self):
        self.assertEqual(decode_mutf8(b"a\xc0\x80b"), "a\0b")
        self.assertEqual(decode_mutf8(b"\xed\xa0\xbd\xed\xb8\x80"), "\U0001f600")
        with self.assertRaises(ClassFormatError):
            decode_mutf8(b"\xff")

    def test_instruction_operands_are_not_treated_as_opcodes(self):
        # bipush's operand happens to equal ldc. Only the actual ldc at 2 is a reference.
        self.assertEqual(list(instructions(bytes([0x10, 0x12, 0x12, 7, 0xb1]))),
                         [(0, None), (2, 7), (4, None)])

    def test_switch_alignment_and_wide_instruction(self):
        table = b"\xaa\0\0\0" + struct.pack(">iiii", 0, 1, 1, 0)
        lookup = b"\xab\0\0\0" + struct.pack(">iiii", 0, 1, 7, 0)
        for switch in (table, lookup):
            code = switch + b"\xc4\x84\0\x01\0\x02\x12\x07\xb1"
            self.assertEqual(list(instructions(code))[-2:], [(26, 7), (28, None)])
        unaligned = b"\0" + table[0:1] + b"\0\0" + table[4:]
        self.assertEqual(list(instructions(unaligned + b"\xb1"))[-1], (20, None))

    def test_malformed_instructions_are_clean_errors(self):
        bad = [b"\x12", b"\xc4", b"\xc4\x12\0\0", b"\xaa", b"\xca",
               b"\xab\0\0\0" + struct.pack(">ii", 0, -1)]
        for code in bad:
            with self.subTest(code=code), self.assertRaises(ClassFormatError):
                list(instructions(code))

    def test_bad_class_headers(self):
        for data in (b"", b"xxxx", b"\xca\xfe\xba\xbe\0\0"):
            with self.assertRaises(ClassFormatError):
                ClassFile(data)


@unittest.skipUnless(shutil.which("javac"), "JDK required for JVM fixtures")
class JvmApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        cls.root = Path(cls.workspace.name)
        cls.classes = cls.root / "classes"
        sources = cls.root / "src" / "sample"
        sources.mkdir(parents=True)
        (sources / "Dependency.java").write_text('''package sample;
public class Dependency {
    public static String token = "fixture-token";
    public static String echo(String input) { return input; }
}
''', encoding="utf-8")
        (sources / "Main.java").write_text('''package sample;
import java.util.function.Function;
public class Main {
    public String run() {
        String value = Dependency.echo("hello-token-😀");
        Function<String, String> fn = Dependency::echo;
        return fn.apply(value) + Dependency.token;
    }
    public int dense(int key) {
        switch(key) { case 1: return 1; case 2: return 2; case 3: return 3; default: return 0; }
    }
    public int sparse(int key) {
        switch(key) { case 1: return 1; case 300: return 2; default: return 0; }
    }
    public static class Inner { public String value() { return "inner-token"; } }
}
''', encoding="utf-8")
        result = subprocess.run([shutil.which("javac"), "--release", "17", "-encoding", "UTF-8",
                                 "-d", str(cls.classes), *map(str, sources.glob("*.java"))],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.main = (cls.classes / "sample/Main.class").read_bytes()
        cls.inner = (cls.classes / "sample/Main$Inner.class").read_bytes()
        cls.dependency = (cls.classes / "sample/Dependency.class").read_bytes()
        cls.library = archive_bytes({"sample/Dependency.class": cls.dependency})
        cls.jar = cls.root / "app.jar"
        cls.jar.write_bytes(archive_bytes({"sample/Main.class": cls.main,
                                         "sample/Main$Inner.class": cls.inner,
                                         "sample/Dependency.class": cls.dependency}))
        cls.war = cls.root / "app.war"
        cls.war.write_bytes(archive_bytes({"WEB-INF/classes/sample/Main.class": cls.main,
                                         "WEB-INF/classes/sample/Main$Inner.class": cls.inner,
                                         "WEB-INF/lib/dependency.jar": cls.library}))

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def cli(self, *args, no_site=False, env=None):
        command = [sys.executable]
        if no_site:
            command.append("-S")
        return subprocess.run([*command, str(ROOT / "main.py"), *map(str, args)], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=60)

    def test_jar_inventory_does_not_read_class_bodies(self):
        original = zipfile.ZipFile.open
        opened = []

        def tracked(archive, name, *args, **kwargs):
            opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
            return original(archive, name, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "open", tracked), JvmApplication(self.jar) as app:
            self.assertEqual(len(list(app.classes())), 3)
        self.assertFalse(any(name.endswith(".class") for name in opened))

    def test_directory_and_nested_war_inventory(self):
        with JvmApplication(self.classes) as app:
            self.assertEqual({entry.name for entry in app.entries},
                             {"sample/Main", "sample/Main$Inner", "sample/Dependency"})
        with JvmApplication(self.war) as app:
            entry = app.select("sample.Dependency")
            self.assertIn("!/WEB-INF/lib/dependency.jar!/sample/Dependency.class", entry.origin)
            self.assertEqual(app.read(entry), self.dependency)
            spool = Path(app._temp.name)
        self.assertFalse(spool.exists())

    def test_boot_jar_and_ear_recursion(self):
        with tempfile.TemporaryDirectory() as temp:
            boot = Path(temp) / "boot.jar"
            boot.write_bytes(archive_bytes({"BOOT-INF/classes/sample/Main.class": self.main,
                                           "BOOT-INF/lib/dep.jar": self.library}))
            ear = Path(temp) / "app.ear"
            ear.write_bytes(archive_bytes({"app.war": self.war.read_bytes()}))
            for path in (boot, ear):
                with JvmApplication(path) as app:
                    self.assertEqual(app.select("sample.Main").name, "sample/Main")
                    self.assertEqual(app.select("sample.Dependency").name, "sample/Dependency")

    def test_duplicate_class_requires_explicit_origin(self):
        with JvmApplication([self.jar, self.war]) as app:
            with self.assertRaisesRegex(ValueError, "Ambiguous class"):
                app.select("sample.Main")
            origins = [entry.origin for entry in app.classes("sample.Main") if entry.name == "sample/Main"]
            self.assertEqual(app.select("Lsample/Main;", origins[1]).origin, origins[1])
            with self.assertRaisesRegex(ValueError, "not found"):
                app.select("sample.Main", "missing-origin")

    def test_multi_release_selection_respects_manifest_and_version(self):
        with tempfile.TemporaryDirectory() as temp:
            jar = Path(temp) / "multi.jar"
            entries = {"sample/Main.class": self.main,
                       "META-INF/versions/11/sample/Main.class": self.main,
                       "META-INF/versions/21/sample/Main.class": self.main}
            for enabled in (True, False):
                if enabled:
                    entries["META-INF/MANIFEST.MF"] = b"Manifest-Version: 1.0\r\nMulti-Release: tr\r\n ue\r\n\r\n"
                else:
                    entries.pop("META-INF/MANIFEST.MF", None)
                jar.write_bytes(archive_bytes(entries))
                for release, expected in ((8, 0), (17, 11), (21, 21)):
                    with JvmApplication(jar, java_release=release) as app:
                        self.assertEqual(app.select("sample.Main").release, expected if enabled else 0)

    def test_bytecode_search_including_method_reference_and_unicode(self):
        with JvmApplication(self.war) as app:
            strings = list(app.findrefs("string", "hello-token-😀"))
            self.assertEqual(len(strings), 1)
            self.assertEqual(strings[0]["method"], "run")
            self.assertEqual(strings[0]["descriptor"], "()Ljava/lang/String;")
            methods = list(app.findrefs("method", "echo", "sample.Dependency"))
            self.assertEqual(len(methods), 2)  # direct invocation and lambda method handle
            self.assertTrue(all(record["class"] == "Lsample/Main;" for record in methods))
            self.assertEqual(len(list(app.findrefs("field", "token", "sample.Dependency"))), 2)
            self.assertTrue(list(app.findrefs("type", "sample.Dependency")))
            self.assertEqual(list(app.findrefs("method", "echo", "Wrong")), [])
            self.assertEqual(len(list(app.findrefs("method", "echo", "Dependency", True))), 2)
            self.assertEqual(list(app.findrefs("string", "hello", prefix="sample.Dependency")), [])

    def test_standalone_class_identity_not_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            renamed = Path(temp) / "renamed.class"
            renamed.write_bytes(self.main)
            with JvmApplication(renamed) as app:
                self.assertEqual(app.entries[0].name, "sample/Main")
                self.assertTrue(list(app.findrefs("string", "hello")))

    def test_cli_json_list_search_and_output_file(self):
        result = self.cli("listclass", self.war, "--json", "--prefix", "sample.Main", no_site=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({record["class"] for record in records}, {"Lsample/Main;", "Lsample/Main$Inner;"})
        self.assertTrue(all(record["origin"].startswith(str(self.war)) for record in records))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "refs.jsonl"
            result = self.cli("findrefs", self.war, "--json", "--debug", "method", "echo",
                              "--class", "sample.Dependency", "-o", output, no_site=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len([json.loads(line) for line in result.stdout.splitlines()]), 2)
            self.assertEqual(output.read_text(), result.stdout)
            self.assertIn("[DEBUG]", result.stderr)
            listing = Path(temp) / "classes.txt"
            result = self.cli("listclass", self.jar, "--origins", "-o", listing)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("\t" + str(self.jar), listing.read_text())

    def test_java8_and_java11_bytecode(self):
        sources = sorted((self.root / "src/sample").glob("*.java"))
        with tempfile.TemporaryDirectory() as temp:
            for release, major in ((8, 52), (11, 55)):
                output = Path(temp) / str(release)
                result = subprocess.run([shutil.which("javac"), "--release", str(release),
                                         "-encoding", "UTF-8", "-d", str(output), *map(str, sources)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                with JvmApplication(output) as app:
                    self.assertEqual(ClassFile(app.read(app.select("sample.Main"))).major, major)
                    self.assertEqual(len(list(app.findrefs("method", "echo", "sample.Dependency"))), 2)
                    self.assertEqual(len(list(app.findrefs("string", "hello-token-😀"))), 1)

    def test_cli_classpath_and_validation(self):
        result = self.cli("listclass", self.jar, "--classpath", self.war, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 6)
        for options, expected in ((["--threads", "0"], "Worker count"),
                                  (["--prefix", " "], "Class prefix"),
                                  (["--java-release", "7"], "Java release")):
            result = self.cli("listclass", self.jar, *options)
            self.assertEqual(result.returncode, 1)
            self.assertIn(expected, result.stderr)

    def test_bad_class_reports_origin_and_spool_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            jar = Path(temp) / "bad.jar"
            jar.write_bytes(archive_bytes({"sample/Main.class": b"bad"}))
            with JvmApplication(jar) as app, self.assertRaisesRegex(ValueError, "bad.jar!/sample/Main.class"):
                list(app.findrefs("string", "token"))
            with patch("droidasc.jvm.application.MAX_NESTED_BYTES", 1), self.assertRaisesRegex(ValueError, "spool limit"):
                JvmApplication(self.war)

    def test_archive_path_traversal_and_name_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            jar = Path(temp) / "bad.jar"
            jar.write_bytes(archive_bytes({"../Escape.class": self.main}))
            with self.assertRaisesRegex(ValueError, "Unsafe class"):
                JvmApplication(jar)
            jar.write_bytes(archive_bytes({"sample/Fake.class": self.main}))
            with JvmApplication(jar) as app, self.assertRaisesRegex(ValueError, "differs from class name"):
                list(app.findrefs("string", "hello"))

    def test_selective_context_includes_nested_dependencies_and_inner_class(self):
        with tempfile.TemporaryDirectory() as temp, JvmApplication(self.war) as app:
            self.assertFalse(app.stage_context(app.select("sample.Main"), temp))
            self.assertEqual({path.name for path in Path(temp).rglob("*.class")},
                             {"Main.class", "Dependency.class", "Main$Inner.class"})

    def test_missing_backend_is_actionable(self):
        with patch.dict(os.environ, {"ASC_CFR_JAR": ""}), JvmApplication(self.jar) as app:
            with self.assertRaisesRegex(ValueError, "--cfr-jar"):
                decompile(app, app.select("sample.Main"))

    @unittest.skipUnless(os.environ.get("ASC_TEST_CFR_JAR"), "Set ASC_TEST_CFR_JAR for real CFR integration")
    def test_real_cfr_decompiles_war_class_and_inner_class(self):
        for name in ("sample.Main", "sample.Main$Inner"):
            result = self.cli("getclass", self.war, name, "--cfr-jar", os.environ["ASC_TEST_CFR_JAR"], "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            self.assertIn("package sample;", record["source"])
            self.assertIn("class ", record["source"])
            self.assertNotIn("Could not load", record["source"])
            self.assertIn("token", record["source"])


class InputRoutingTests(unittest.TestCase):
    def test_zip_dex_keeps_android_route(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "app.zip"
            path.write_bytes(archive_bytes({"classes.dex": b"dex\n"}))
            self.assertFalse(is_jvm_input(path))
            path.write_bytes(archive_bytes({"sample/Main.class": b"class"}))
            self.assertTrue(is_jvm_input(path))


if __name__ == "__main__":
    unittest.main()
