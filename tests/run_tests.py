"""Run self-contained regression tests without an external APK."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-decompiler', action='store_true')
    parser.add_argument('--require-jvm', action='store_true', help='Require javac and real CFR integration')
    parser.add_argument('--suite', choices=('all', 'unit', 'integration'), default='all')
    args = parser.parse_args()
    if args.require_decompiler and importlib.util.find_spec('dadguard') is None:
        parser.error('dadguard is required; check the source tree')
    if args.require_jvm and (not shutil.which('javac') or not os.environ.get('ASC_TEST_CFR_JAR')):
        parser.error('--require-jvm needs javac and ASC_TEST_CFR_JAR pointing to a CFR JAR')
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
    if args.suite != 'all':
        def tests(items):
            for item in items:
                if isinstance(item, unittest.TestSuite):
                    yield from tests(item)
                else:
                    yield item
        suite = unittest.TestSuite(test for test in tests(suite)
                                   if ('test_decompiler.DecompilerTests.' in test.id()
                                       or test.id().endswith('test_archive_is_reproducible_and_runs_outside_checkout')
                                       or test.id().endswith('test_real_cfr_decompiles_war_class_and_inner_class')
                                       or test.id().endswith('test_real_cfr_bulk_export_and_resume')
                                       or (test.id().startswith(('test_jvm_batch.', 'test_jvm_worker.')) and '.test_real_cfr_' in test.id()))
                                   == (args.suite == 'integration'))
    if suite.countTestCases() == 0:
        parser.error('selected suite contains no tests')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    required_skips = [test for test, _reason in result.skipped
                      if (args.require_jvm if test.id().startswith(('test_jvm.', 'test_jvm_web.', 'test_jvm_batch.', 'test_jvm_worker.')) else args.require_decompiler)]
    sys.exit(0 if result.wasSuccessful() and not required_skips else 1)
