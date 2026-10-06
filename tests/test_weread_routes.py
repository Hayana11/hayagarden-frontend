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
    def test_shelf_uses_official_nested_album_shape_and_visible_count(self):
        fake = FakeClient(responses={
            "/shelf/sync": {
                "data": {
                    "books": [{
                        "bookId": "b1",
                        "title": "Book",
                        "author": "Author",
                        "cover": "https://img.example/1",
                        "category": ["classic"],
                        "readUpdateTime": 123,
                        "finishReading": 0,
                        "isTop": 1,
                        "secret": False,
                        "deepLink": "weread://b1",
                        "privateField": "must not escape",
                    }],
                    "albums": [
                        {
                            "albumInfo": {
                                "albumId": "a1",
                                "name": "Album",
                                "authorName": "Narrator",
                                "cover": "https://img.example/a1",
                                "trackCount": 12,
                                "finish": 1,
                            },
                            "albumInfoExtra": {
                                "secret": 1,
                                "lectureReadUpdateTime": 456,
                                "isTop": 1,
                            },
                        },
                        # A legacy/incorrect flat object must not be parsed.
                        {"albumId": "flat", "name": "Not official shape"},
                    ],
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
        self.assertEqual(body["albums"], [{
            "albumId": "a1",
            "title": "Album",
            "author": "Narrator",
            "cover": "https://img.example/a1",
            "trackCount": 12,
            "finish": True,
            "secret": True,
            "lectureReadUpdateTime": 456,
            "isTop": True,
        }])
        self.assertTrue(body["hasArticleCollection"])
        self.assertEqual(fake.calls, [("/shelf/sync", {})])

    def test_progress_reads_official_nested_book_and_preserves_1_and_100(self):
        fake = FakeClient(responses={
            "/book/getprogress": {
                "data": {
                    "book": {
                        "bookId": "b1",
                        "chapterUid": "ch-7",
                        "chapterOffset": 88,
                        "progress": 1,
                        "updateTime": 100,
                        "recordReadingTime": 200,
                        "finishTime": 0,
                        "isStartReading": 1,
                    },
                },
            },
        })
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            one = make_app(fake).test_client().get("/api/weread/books/b1/progress").get_json()
        self.assertEqual(one, {
            "bookId": "b1",
            "chapterUid": "ch-7",
            "chapterOffset": 88,
            "progress": 1,
            "updateTime": 100,
            "recordReadingTime": 200,
            "finishTime": 0,
            "isStartReading": True,
        })
        self.assertNotEqual(one["progress"], 100)

        fake.responses["/book/getprogress"] = {
            "result": {
                "book": {
                    "bookId": "b1",
                    "chapterUid": "ch-8",
                    "chapterOffset": 99,
                    "progress": 100,
                    "updateTime": 300,
                    "recordReadingTime": 400,
                    "finishTime": 500,
                    "isStartReading": 1,
                },
            },
        }
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            done = make_app(fake).test_client().get("/api/weread/books/b1/progress").get_json()
        self.assertEqual(done["chapterUid"], "ch-8")
        self.assertEqual(done["progress"], 100)
        self.assertEqual(done["finishTime"], 500)

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

    def test_notes_use_updated_and_nested_review_shape(self):
        fake = FakeClient(responses={
            "/book/bookmarklist": {
                "data": {
                    "updated": [{
                        "bookmarkId": "m1",
                        "bookId": "b1",
                        "chapterUid": "ch-1",
                        "markText": "quote",
                        "createTime": 100,
                        "range": "1,2",
                        "colorStyle": 3,
                        "type": 0,
                    }],
                },
            },
            "/review/list/mine": {
                "result": {
                    "reviews": [{
                        "review": {
                            "reviewId": "r1",
                            "content": "thought",
                            "abstract": "review quote",
                            "range": "8,13",
                            "chapterUid": "ch-2",
                            "chapterIdx": 4,
                            "createTime": 200,
                            "star": 1,
                            "chapterName": "Chapter 2",
                            "isFinish": 1,
                        },
                    }],
                },
            },
        })
        with patch.dict(os.environ, {"WEREAD_API_KEY": "test-key"}, clear=True):
            response = make_app(fake).test_client().get("/api/weread/books/b1/notes")
        self.assertEqual(response.status_code, 200)
        notes = response.get_json()["notes"]
        self.assertEqual([x["id"] for x in notes], ["m1", "r1"])
        self.assertEqual(notes[0]["type"], "bookmark")
        self.assertEqual(notes[0]["quote"], "quote")
        self.assertEqual(notes[0]["text"], "")
        self.assertEqual(notes[0]["colorStyle"], 3)
        self.assertEqual(notes[1]["type"], "review")
        self.assertEqual(notes[1]["quote"], "review quote")
        self.assertEqual(notes[1]["text"], "thought")
        self.assertEqual(notes[1]["chapterIdx"], 4)
        self.assertEqual(notes[1]["chapterName"], "Chapter 2")
        self.assertTrue(notes[1]["isFinish"])
        self.assertEqual(
            fake.calls,
            [
                ("/book/bookmarklist", {"bookId": "b1"}),
                ("/review/list/mine", {"bookid": "b1"}),
            ],
        )

    def test_client_request_flattens_business_params_and_protects_protocol_fields(self):
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
        result = WereadClient("test-key", opener=opener).call(
            "/book/getprogress",
            {
                "bookId": "b1",
                "api_name": "evil",
                "skill_version": "evil",
            },
        )
        body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(body, {
            "api_name": "/book/getprogress",
            "bookId": "b1",
            "skill_version": "1.0.4",
        })
        self.assertNotIn("params", body)
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer test-key")

    def test_gateway_business_errors_are_structured_and_do_not_leak_secrets(self):
        secret = "test-secret-never-returned"

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def read(self, _size):
                return json.dumps(self.payload).encode("utf-8")

        class Opener:
            def __init__(self, payload):
                self.payload = payload

            def open(self, _request, timeout):
                return Response(self.payload)

        for payload, code in [
            ({"errcode": 1001, "message": secret}, "WEREAD_BUSINESS_ERROR"),
            ({"errcode": 1002, "upgrade_info": {"message": secret}}, "WEREAD_UPGRADE_REQUIRED"),
        ]:
            with self.assertRaises(WereadError) as raised:
                WereadClient(secret, opener=Opener(payload)).call("/shelf/sync")
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(secret, str(raised.exception))

        fake = FakeClient(errors={"/shelf/sync": WereadError("WEREAD_UPGRADE_REQUIRED", 502)})
        with patch.dict(os.environ, {"WEREAD_API_KEY": secret}, clear=True):
            response = make_app(fake).test_client().get("/api/weread/shelf")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {
            "ok": False,
            "code": "WEREAD_UPGRADE_REQUIRED",
            "upgradeRequired": True,
        })
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_transport_failure_never_leaks_api_key(self):
        secret = "test-secret-never-returned"

        class Broken:
            def open(self, _request, timeout):
                raise URLError(secret)

        with self.assertRaises(WereadError) as raised:
            WereadClient(secret, opener=Broken()).call("/shelf/sync")
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
