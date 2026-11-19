"""The public privacy policy page: static content checks plus the /privacy route."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, 'static', 'privacy.html')
sys.path.insert(0, ROOT)

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


class PrivacyPageContentTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(PAGE))
        with open(PAGE, encoding='utf-8') as f:
            self.html = f.read()

    def test_has_both_titles(self):
        self.assertIn('隐私政策', self.html)
        self.assertIn('Privacy Policy', self.html)

    def test_no_at_sign_so_no_email(self):
        self.assertNotIn('@', self.html)

    def test_no_script_tag(self):
        self.assertNotIn('<script', self.html.lower())

    def test_required_links(self):
        self.assertIn('https://developers.google.com/terms/api-services-user-data-policy', self.html)
        self.assertIn('https://myaccount.google.com/permissions', self.html)


@unittest.skipUnless(HAVE_FLASK, 'app.py needs flask')
class PrivacyRouteTests(unittest.TestCase):
    def test_privacy_route_is_public_html(self):
        import app as app_module
        client = app_module.app.test_client()
        resp = client.get('/privacy')
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertIn('text/html', resp.headers.get('Content-Type', ''))
        finally:
            resp.close()


if __name__ == '__main__':
    unittest.main()
