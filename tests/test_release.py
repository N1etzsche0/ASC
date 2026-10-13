import hashlib
import json
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
import zipfile

from dex_fixture import make_dex
from scripts.build_release import build


class ReleaseTests(unittest.TestCase):
    def test_invalid_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for version in ('0.1.0', '../v0.1.0', 'v01.0.0', 'v0.1.0-rc.0'):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    build(version, Path(directory))

    def test_archive_is_reproducible_and_runs_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = build('v0.1.0-rc.1', root)
            original = archive.read_bytes()
            self.assertEqual(build('v0.1.0-rc.1', root).read_bytes(), original)
            self.assertEqual((root / 'SHA256SUMS').read_text().split()[0],
                             hashlib.sha256(original).hexdigest())
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                self.assertTrue(any(name.endswith('/requirements.txt') for name in names))
                self.assertIn('ASC-v0.1.0-rc.1/LICENSE', names)
                self.assertIn('ASC-v0.1.0-rc.1/droidasc/jvm/application.py', names)
                self.assertIn('ASC-v0.1.0-rc.1/droidasc/jvm/memory.py', names)
                self.assertIn('ASC-v0.1.0-rc.1/droidasc/jvm/CfrWorker.java', names)
                self.assertIn('ASC-v0.1.0-rc.1/docs/JVM-ARCHITECTURE.md', names)
                self.assertIn('ASC-v0.1.0-rc.1/tests/TESTING.md', names)
                self.assertFalse(any(name.endswith(('.dex', '.apk', '/test.py', '/test_findrefs.py'))
                                     or ('/tests/' in name and not name.endswith('/tests/TESTING.md'))
                                     or '/.git/' in name for name in names))
                package.extractall(root)
            app = root / 'ASC-v0.1.0-rc.1'
            apk = root / 'fixture.apk'
            with zipfile.ZipFile(apk, 'w', compression=zipfile.ZIP_DEFLATED) as package:
                package.writestr('classes.dex', make_dex())
            for args, expected in ((['listclass', str(apk), '--prefix', 'example'], 'Lexample/Test;'),
                                   (['findrefs', str(apk), 'string', 'token'], 'token'),
                                   (['getclass', str(apk), 'example.Test'], 'class Test')):
                result = subprocess.run([sys.executable, str(app / 'main.py'), *args],
                                        cwd=root, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
            name = b'example/Test'
            # Minimal valid class: UTF8 name, Class(name), no members or attributes.
            data = (b'\xca\xfe\xba\xbe' + struct.pack('>HHH', 0, 52, 3)
                    + b'\x01' + struct.pack('>H', len(name)) + name + b'\x07\x00\x01'
                    + struct.pack('>HHHHHHH', 1, 2, 0, 0, 0, 0, 0))
            jar = root / 'fixture.jar'
            with zipfile.ZipFile(jar, 'w') as package:
                package.writestr('example/Test.class', data)
            result = subprocess.run([sys.executable, '-S', str(app / 'main.py'),
                                     'listclass', str(jar), '--json'], cwd=root,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['class'], 'Lexample/Test;')
