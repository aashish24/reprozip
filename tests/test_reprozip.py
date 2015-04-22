import os
from rpaths import Path
import unittest

from reprozip.tracer.trace import UniqueNames, label_files
from reprozip.utils import make_dir_writable


class TestReprozip(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'chown'), "No POSIX file permissions")
    def test_make_dir_writable(self):
        """Tests make_dir_writable with read-only dir."""
        def check_mode(mod, path):
            self.assertEqual(oct((path.stat().st_mode & 0o0700) >> 6),
                             oct(mod))

        tmp = Path.tempdir()
        try:
            (tmp / 'some' / 'path').mkdir(parents=True)
            (tmp / 'some' / 'path').chmod(0o555)
            with make_dir_writable(tmp / 'some' / 'path'):
                check_mode(7, tmp / 'some')
                check_mode(7, tmp / 'some' / 'path')
            check_mode(7, tmp / 'some')
            check_mode(5, tmp / 'some' / 'path')
        finally:
            (tmp / 'some').chmod(0o755)
            (tmp / 'some' / 'path').chmod(0o755)
            tmp.rmtree()

    @unittest.skipUnless(hasattr(os, 'chown'), "No POSIX file permissions")
    def test_make_dir_writable2(self):
        """Tests make_dir_writable with read-only and no-executable dirs."""
        def check_mode(mod, path):
            self.assertEqual(oct((path.stat().st_mode & 0o0700) >> 6),
                             oct(mod))

        tmp = Path.tempdir()
        try:
            (tmp / 'some' / 'complete' / 'path').mkdir(parents=True)
            (tmp / 'some' / 'complete' / 'path').chmod(0o555)
            (tmp / 'some' / 'complete').chmod(0o444)
            with make_dir_writable(tmp / 'some' / 'complete' / 'path'):
                check_mode(7, tmp / 'some')
                check_mode(7, tmp / 'some' / 'complete')
                check_mode(7, tmp / 'some' / 'complete' / 'path')
            check_mode(7, tmp / 'some')
            check_mode(4, tmp / 'some' / 'complete')
            (tmp / 'some' / 'complete').chmod(0o755)
            check_mode(5, tmp / 'some' / 'complete' / 'path')
        finally:
            (tmp / 'some').chmod(0o755)
            (tmp / 'some' / 'complete').chmod(0o755)
            (tmp / 'some' / 'complete' / 'path').chmod(0o755)
            tmp.rmtree()


class TestNames(unittest.TestCase):
    def test_uniquenames(self):
        """Tests UniqueNames."""
        u = UniqueNames()
        self.assertEqual(u('test'), 'test')
        self.assertEqual(u('test'), 'test_2')
        self.assertEqual(u('test'), 'test_3')
        self.assertEqual(u('test_2'), 'test_2_2')
        self.assertEqual(u('test_'), 'test_')
        self.assertEqual(u('test_'), 'test__2')

    def test_label_files(self):
        """Tests input/output file labelling."""
        wd = Path('/fakeworkingdir')
        self.assertEqual(
                label_files(
                    [{'argv': ['aa', 'bb.txt'], 'workingdir': wd}],
                    [[wd / 'aa', Path('/other/cc.bin'), wd / 'bb.txt']],
                    'input'),
                {'arg0': wd / 'aa',
                 'cc.bin': Path('/other/cc.bin'),
                 'arg1': wd / 'bb.txt'})
