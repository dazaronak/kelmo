"""Run with a stdlib-only controller outside the child OS sandbox. The macOS kernel boundary itself is
tests/test_runtime_macos.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from kelmo.runtime import IsolationError, RuntimePaths, command, macos_policy


class RuntimeTests(unittest.TestCase):
    def test_homebrew_loader_alias_gets_metadata_only_for_a_granted_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            library = root / 'Cellar/sqlite/1/lib'
            library.mkdir(parents=True)
            alias = root / 'opt/sqlite'
            alias.parent.mkdir()
            alias.symlink_to(library.parent, target_is_directory=True)
            policy = macos_policy(RuntimePaths((library,), (), ()))
            self.assertIn('(allow file-read-metadata (literal ' + json.dumps(str(alias)) + '))', policy)
            self.assertNotIn('(allow file-read* (subpath ' + json.dumps(str(alias)), policy)
            self.assertNotIn('(allow file-read* (literal ' + json.dumps(str(alias)), policy)

    def test_homebrew_alias_pointing_outside_granted_libraries_gets_no_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            library = root / 'Cellar/sqlite/1/lib'
            library.mkdir(parents=True)
            private = root / 'private'
            private.mkdir()
            alias = root / 'opt/sqlite'
            alias.parent.mkdir()
            alias.symlink_to(private, target_is_directory=True)
            policy = macos_policy(RuntimePaths((library,), (), ()))
            self.assertNotIn(json.dumps(str(alias)), policy)

    def test_unsupported_platform_fails_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = RuntimePaths((Path(sys.base_prefix).resolve(),),
                                 (Path(directory).resolve(),),
                                 (Path(sys.executable).resolve(),))
            with self.assertRaises(IsolationError):
                command(paths, [sys.executable, '-V'], platform='unsupported')

    def test_data_cannot_cover_the_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = RuntimePaths((root / 'code',), (root,), ())
            with self.assertRaises(IsolationError):
                paths.validate()
