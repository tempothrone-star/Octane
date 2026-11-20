"""Build overrides allow dependency changes while original test settings stay fixed."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from octane.build_config import build_change_review, is_build_path
from octane.test_protection import baseline_tests, is_test_path


class BuildConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.original, self.candidate = self.base/'original', self.base/'candidate'
        self.original.mkdir(); self.candidate.mkdir()

    def files(self, name, before, after):
        (self.original/name).write_text(before)
        (self.candidate/name).write_text(after)

    def review(self, path, **kwargs):
        return build_change_review(self.original, self.candidate, [path], **kwargs)

    def test_build_paths_are_separate_from_pure_tests(self):
        for path in ('package.json', 'pyproject.toml', 'Cargo.toml', 'pom.xml', 'build.gradle.kts', 'tox.ini'):
            self.assertTrue(is_build_path(path))
            self.assertFalse(is_test_path(path))
        self.assertTrue(is_test_path('tests/package.json'))
        self.files('package.json', '{}', '{}')
        self.files('test_app.py', 'assert False', 'assert True')
        protection, mounts = baseline_tests(self.original)
        self.assertEqual(mounts, ['test_app.py'])
        self.assertEqual(protection['file_count'], 1)

    def test_package_dependency_bump_requires_override_and_records_only_changed_keys(self):
        before = {'scripts': {'test': 'node --test'}, 'dependencies': {'example': '1.0'}}
        after = {**before, 'dependencies': {'example': '1.1'}}
        self.files('package.json', json.dumps(before), json.dumps(after))
        for options in ({}, {'allowed': True}, {'allowed': 'yes', 'rationale': 'fix'}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.review('package.json', **options)
        result = self.review('package.json', allowed=True, rationale='Fix the documented dependency regression')
        self.assertEqual(result['files'][0]['key_diff'][0]['key'], '/dependencies/example')
        self.assertEqual(len(result['files'][0]['key_diff']), 1)
        self.assertTrue(result['allowed'])

    def test_build_override_cannot_weaken_test_keys(self):
        cases = (
            ('package.json', '{"scripts":{"test":"node --test"}}', '{"scripts":{"test":"true"}}'),
            ('pyproject.toml', '[tool.pytest.ini_options]\naddopts=""\n', '[tool.pytest.ini_options]\naddopts="--ignore=tests"\n'),
            ('Cargo.toml', '[package]\nname="x"\nautotests=true\n', '[package]\nname="x"\nautotests=false\n'),
            ('tox.ini', '[testenv]\ncommands=pytest\n', '[testenv]\ncommands=true\n'),
        )
        for name, before, after in cases:
            self.files(name, before, after)
            with self.subTest(path=name):
                with self.assertRaisesRegex(ValueError, 'test settings'):
                    self.review(name, allowed=True, rationale='cannot hide a failure')

    def test_toml_build_changes_and_opaque_text_reviews_are_explicit(self):
        self.files('pyproject.toml', '[project]\nversion="1"\n', '[project]\nversion="2"\n')
        self.assertEqual(self.review('pyproject.toml', allowed=True, rationale='fix metadata')['files'][0]['key_diff'][0]['key'], '/project/version')
        self.files('build.gradle', 'version = "1"', 'version = "2"')
        review = self.review('build.gradle', allowed=True, rationale='fix build')
        self.assertIsNone(review['files'][0]['key_diff'])
        self.assertIn('independent', review['files'][0]['review_basis'])
        self.files('package.json', '{}', 'bad json')
        with self.assertRaisesRegex(ValueError, 'structurally review'):
            self.review('package.json', allowed=True, rationale='malformed')
