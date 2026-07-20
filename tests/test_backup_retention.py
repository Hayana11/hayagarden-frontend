import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tools.backup_retention import (
    keep_frontend_tars,
    keep_runtime_dirs,
    load_anchors,
    prune,
)


class BackupRetentionTests(unittest.TestCase):
    def test_keep_newest_three_deploy_tars(self):
        names = [
            'frontend-20260718-200003.tar.gz',
            'frontend-20260719-155221.tar.gz',
            'frontend-20260720-032451.tar.gz',
            'frontend-20260720-032548.tar.gz',
            'frontend-20260710-200001.tar.gz',
        ]
        now = datetime(2026, 7, 20, 4, 0, 0)
        keep = keep_frontend_tars(names, now)
        self.assertIn('frontend-20260720-032548.tar.gz', keep)
        self.assertIn('frontend-20260720-032451.tar.gz', keep)
        self.assertIn('frontend-20260719-155221.tar.gz', keep)
        self.assertNotIn('frontend-20260710-200001.tar.gz', keep)

    def test_keep_one_per_day_last_seven_days(self):
        names = [
            'frontend-20260714-040706.tar.gz',
            'frontend-20260714-200002.tar.gz',
            'frontend-20260718-200003.tar.gz',
            'frontend-20260719-155221.tar.gz',
            'frontend-20260720-032548.tar.gz',
            'frontend-20260701-200002.tar.gz',
        ]
        now = datetime(2026, 7, 20, 4, 0, 0)
        keep = keep_frontend_tars(names, now)
        self.assertIn('frontend-20260714-200002.tar.gz', keep)
        self.assertNotIn('frontend-20260714-040706.tar.gz', keep)
        self.assertNotIn('frontend-20260701-200002.tar.gz', keep)

    def test_keep_monthly_anchor(self):
        names = [
            'frontend-20260619-200002.tar.gz',
            'frontend-20260630-200002.tar.gz',
            'frontend-20260713-160048.tar.gz',
        ]
        now = datetime(2026, 7, 20, 4, 0, 0)
        keep = keep_frontend_tars(names, now)
        self.assertIn('frontend-20260630-200002.tar.gz', keep)
        self.assertIn('frontend-20260713-160048.tar.gz', keep)

    def test_keep_three_runtime_dirs(self):
        names = [
            'predeploy-runtime-20260713-160048',
            'predeploy-runtime-20260718-175902',
            'predeploy-runtime-20260720-032548',
            'predeploy-runtime-20260711-112030',
        ]
        keep = keep_runtime_dirs(names)
        self.assertEqual(
            keep,
            {
                'predeploy-runtime-20260720-032548',
                'predeploy-runtime-20260718-175902',
                'predeploy-runtime-20260713-160048',
            },
        )

    def test_anchors_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            anchors = base / 'anchors.txt'
            anchors.write_text(
                'frontend-20260630-200002.tar.gz\n'
                'predeploy-runtime-20260713-160048\n',
                encoding='utf-8',
            )
            old_tar = base / 'frontend-20260630-200002.tar.gz'
            old_tar.write_bytes(b'x')
            new_tar = base / 'frontend-20260720-032548.tar.gz'
            new_tar.write_bytes(b'y')
            runtime = base / 'predeploy-runtime-20260713-160048'
            runtime.mkdir()
            (runtime / 'marker').write_text('keep', encoding='utf-8')

            result = prune(
                base,
                anchors,
                dry_run=True,
                now=datetime(2026, 7, 20, 4, 0, 0),
            )
            deleted_names = {name for _, name in result['deleted']}
            self.assertNotIn('frontend-20260630-200002.tar.gz', deleted_names)
            self.assertNotIn('predeploy-runtime-20260713-160048', deleted_names)

    def test_manual_dirs_not_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manual = base / 'moments-predeploy-20260716-113438'
            manual.mkdir()
            (manual / 'memories.db').write_bytes(b'db')
            anchors = base / 'anchors.txt'
            anchors.write_text('', encoding='utf-8')
            prune(base, anchors, dry_run=False, now=datetime(2026, 7, 20))
            self.assertTrue(manual.exists())

    def test_load_anchors_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'anchors.txt'
            path.write_text('# comment\nfoo.tar.gz\n', encoding='utf-8')
            self.assertEqual(load_anchors(path), {'foo.tar.gz'})


if __name__ == '__main__':
    unittest.main()
