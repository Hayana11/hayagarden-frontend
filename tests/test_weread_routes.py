import json
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

from flask import Flask

from weread_client import WereadClient, WereadError
from weread_routes import create_weread_blueprint


class FakeClient:
    def __init__(self, responses=None, errors=None):
        self.responses = responses or {}
        self.errors = errors or {}
        self.calls = []

    def call(self, api_name, params=None):
        self.calls.append((api_name, params or {}))
        if api_name in self.errors:
            raise self.errors[api_name]
        return self.responses.get(api_name, {})


def make_app(fake):
    app = Flask(__name__)
    app.register_blueprint(create_weread_blueprint(lambda _key: fake))
    return app


class WeReadRouteTests(unittest.TestCase):
    def test_shelf_is_trimmed_and_albums_stay_separate(self):
        fake = FakeClient(responses={
            "/shelf/sync": {
                "data": {
                    "books": [{
                        "bookId": "b1",
                        "title": "Book",
                        "author": "Author",
                        "cover": "https://img.example/1",
                        "category": "classic",
                        "readUpdateTime": 123,
                        "finishReading": 0,
                        "isTop": 1,
                        "secret": False,
                        "deepLink": "weread://b1",
                        "privateField": "must not escape",
                    }],
                    "albums": [{"albumId": "a1", "title": "Album", "hidden": "trim"}],
                    "mp": {"id": "articles"},
                },
            },
        })
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            response = make_app(fake).test_client().get("/api/weread/shelf")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["totalVisibleCount"], 3)
        self.assertEqual(body["books"][0]["bookId"], "b1")
        self.assertNotIn("privateField", body["books"][0])
        self.assertEqual(body["albums"][0]["albumId"], "a1")
        self.assertTrue(body["hasArticleCollection"])
        self.assertEqual(fake.calls, [("/shelf/sync", {})])

    def test_progress_one_is_not_done_and_100_is_done(self):
        fake = FakeClient(responses={"/book/getprogress": {"bookId": "b1", "progress": 1}})
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            one = make_app(fake).test_client().get("/api/weread/books/b1/progress").get_json()
        self.assertEqual(one["progress"], 1)
        self.assertNotEqual(one["progress"], 100)

        fake.responses["/book/getprogress"] = {"bookId": "b1", "progress": 100}
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            done = make_app(fake).test_client().get("/api/weread/books/b1/progress").get_json()
        self.assertEqual(done["progress"], 100)

    def test_missing_key_is_structured_503(self):
        with patch.dict(os.environ, {}, clear=True):
            response = make_app(FakeClient()).test_client().get("/api/weread/shelf")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_NOT_CONFIGURED"})

    def test_upstream_statuses_are_safe(self):
        for status, code, expected_status in [
            (401, "WEREAD_UNAUTHORIZED", 502),
            (403, "WEREAD_UNAUTHORIZED", 502),
            (429, "WEREAD_RATE_LIMITED", 429),
            (500, "WEREAD_UPSTREAM_ERROR", 502),
        ]:
            fake = FakeClient(errors={"/shelf/sync": WereadError(code, expected_status)})
            with patch.dict(os.environ, {"WEREAD_API_KEY": "super-secret"}, clear=True):
                response = make_app(fake).test_client().get("/api/weread/shelf")
            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(response.get_json(), {"ok": False, "code": code})
            self.assertNotIn("super-secret", response.get_data(as_text=True))

    def test_notes_call_both_sources_and_map_we_read_author_to_haya(self):
        fake = FakeClient(responses={
            "/book/bookmarklist": {"data": {"bookmarks": [{"bookmarkId": "m1", "markText": "quote"}]}},
            "/review/list/mine": {"data": {"reviews": [{"reviewId": "r1", "content": "thought"}]}},
        })
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            response = make_app(fake).test_client().get("/api/weread/books/b1/notes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([x["who"] for x in response.get_json()["notes"]], ["haya", "haya"])
        self.assertEqual(
            [x[0] for x in fake.calls],
            ["/book/bookmarklist", "/review/list/mine"],
        )

    def test_client_request_contains_version_but_never_leaks_key_on_failure(self):
        secret = "test-secret-never-returned"
        class Response:
            status = 200
            def read(self, _size):
                return b'{"ok":true}'
        class Opener:
            def __init__(self):
                self.request = None
            def open(self, request, timeout):
                self.request = request
                return Response()
        opener = Opener()
        result = WereadClient(secret, opener=opener).call("/shelf/sync")
        body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(body["skill_version"], "1.0.4")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer " + secret)

        class Broken:
            def open(self, _request, timeout):
                raise URLError(secret)
        with self.assertRaises(WereadError) as raised:
            WereadClient(secret, opener=Broken()).call("/shelf/sync")
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
