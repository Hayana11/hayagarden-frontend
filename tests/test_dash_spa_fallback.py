"""Dash SPA deep-link fallback contract (Phase 1 navigation).

Mirrors production /dash, /dash/, and /dash/<path> serving rules without
importing the full app.py (which binds /opt/frontend at import time).
Production app.py must register both /dash and /dash/ explicitly.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flask import Flask, jsonify, send_from_directory


def build_dash_app(dist_dir: Path, static_dir: Path) -> Flask:
    app = Flask(__name__)
    dist = str(dist_dir)
    static = str(static_dir)

    @app.route('/dash')
    @app.route('/dash/')
    def dash():
        if (dist_dir / 'index.html').is_file():
            resp = send_from_directory(dist, 'index.html')
            resp.headers['Cache-Control'] = 'no-store, must-revalidate'
            return resp
        return send_from_directory(static, 'dash.html')

    @app.route('/dash/<path:subpath>')
    def dash_subpath(subpath: str):
        if not (dist_dir / 'index.html').is_file():
            return send_from_directory(static, 'dash.html')
        asset_path = dist_dir / subpath
        if asset_path.is_file():
            return send_from_directory(dist, subpath)
        if subpath == '__continuity' or subpath.startswith('__continuity/'):
            resp = jsonify({'ok': False, 'error': 'not_found'})
            resp.status_code = 404
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        resp = send_from_directory(dist, 'index.html')
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        return resp

    @app.route('/read')
    def read_page():
        return send_from_directory(static, 'read.html')

    @app.route('/board')
    def board_page():
        return send_from_directory(static, 'board.html')

    return app


SPA_MARKER = '<!-- DASH_SPA_INDEX -->'
READ_MARKER = '<!-- READ_STATIC -->'
BOARD_MARKER = '<!-- BOARD_STATIC -->'


class DashSpaFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix='dash-spa-fallback-')
        root = Path(self._tmp.name)
        self.dist = root / 'dist'
        self.static = root / 'static'
        self.dist.mkdir()
        self.static.mkdir()
        (self.dist / 'index.html').write_text(f'<html>{SPA_MARKER}</html>', encoding='utf-8')
        (self.dist / 'assets').mkdir()
        (self.dist / 'assets' / 'app.js').write_text('console.log(1)', encoding='utf-8')
        (self.static / 'dash.html').write_text('<html>legacy-dash</html>', encoding='utf-8')
        (self.static / 'read.html').write_text(f'<html>{READ_MARKER}</html>', encoding='utf-8')
        (self.static / 'board.html').write_text(f'<html>{BOARD_MARKER}</html>', encoding='utf-8')
        self.client = build_dash_app(self.dist, self.static).test_client()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_spa(self, path: str) -> None:
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        body = resp.get_data(as_text=True)
        self.assertIn(SPA_MARKER, body, path)
        self.assertNotIn(READ_MARKER, body, path)
        self.assertNotIn(BOARD_MARKER, body, path)

    def test_dash_root_and_deep_links_serve_spa_index(self) -> None:
        for path in (
            '/dash',
            '/dash/',
            '/dash/contacts',
            '/dash/chat',
            '/dash/settings',
            '/dash/group-chat',
            '/dash/profile',
            '/dash/memory',
        ):
            with self.subTest(path=path):
                self._assert_spa(path)

    def test_dash_assets_still_served_as_files(self) -> None:
        resp = self.client.get('/dash/assets/app.js')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('console.log(1)', resp.get_data(as_text=True))
        self.assertNotIn(SPA_MARKER, resp.get_data(as_text=True))

    def test_continuity_prefix_is_not_swallowed_by_spa_html(self) -> None:
        for path in (
            '/dash/__continuity/blocks',
            '/dash/__continuity/current',
            '/dash/__continuity/settings',
        ):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 404, path)
                self.assertEqual(resp.get_json(), {'ok': False, 'error': 'not_found'})
                self.assertEqual(resp.headers.get('Cache-Control'), 'no-store')
                self.assertNotIn(SPA_MARKER, resp.get_data(as_text=True))

    def test_read_and_board_not_swallowed_by_dash_fallback(self) -> None:
        read = self.client.get('/read')
        self.assertEqual(read.status_code, 200)
        self.assertIn(READ_MARKER, read.get_data(as_text=True))
        self.assertNotIn(SPA_MARKER, read.get_data(as_text=True))

        board = self.client.get('/board')
        self.assertEqual(board.status_code, 200)
        self.assertIn(BOARD_MARKER, board.get_data(as_text=True))
        self.assertNotIn(SPA_MARKER, board.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
