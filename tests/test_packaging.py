"""Regression tests for the public release boundary and importer integrity."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_release
from scripts.import_data import copy_verified
from scripts.validate_data import validate


class PackagingTests(unittest.TestCase):
    def test_import_does_not_overwrite_different_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source.json', root / 'copy.json'
            source.write_text('{"synthetic": true}')
            record = copy_verified(source, target)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(len(record['sha256']), 64)
            self.assertEqual(copy_verified(source, target), record)
            target.write_text('changed')
            with self.assertRaises(ValueError):
                copy_verified(source, target)
            self.assertEqual(target.read_text(), 'changed')

    def test_release_scanner_rejects_force_added_private_data(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(check_release, 'ROOT', Path(directory)):
            path = Path(directory) / 'data/bench/private.json'
            path.parent.mkdir(parents=True)
            path.write_text('{}')
            self.assertTrue(check_release.audit(['data/bench/private.json']))

    def test_release_scanner_rejects_identifier_payload(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(check_release, 'ROOT', Path(directory)):
            path = Path(directory) / 'unexpected.json'
            path.write_text(json.dumps({'subject_id': 123}))
            self.assertTrue(check_release.audit(['unexpected.json']))

    def test_all_synthetic_cases_are_valid(self):
        root = Path(__file__).resolve().parents[1] / 'data/synthetic'
        stats, errors = validate(root)
        self.assertEqual(errors, [])
        self.assertEqual(sum(item['cases'] for item in stats.values()), 4)
