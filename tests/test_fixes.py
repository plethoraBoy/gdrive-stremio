"""Regression tests for the intermittent-failure bugs (no network required).

Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sgd.cache import Json
from sgd.gdrive import GoogleDrive
from sgd.meta import IMDb, Meta
from sgd.routes import get_streams
from sgd.streams import Streams
from sgd.utils import hr_size, req_api


class FakeDrive:
    def __init__(self, results):
        self.results = results
        self.drive_names = SimpleNamespace(contents={})

    def get_acc_token(self):
        return "fake-token"


class FakeMeta:
    def __init__(self, stream_type="movie", year="2020", titles=None):
        self.type = stream_type
        self.stream_type = stream_type
        self.year = year
        self.titles = titles or ["Test Movie"]
        self.se = "02"
        self.ep = "05"


class QueryTests(unittest.TestCase):
    def test_escapes_apostrophes_and_ignores_empty_words(self):
        query = GoogleDrive.qgen("O'Brien  Movie")
        self.assertIn("name contains 'O\\'Brien'", query)
        self.assertNotIn("contains ''", query)

    def test_movie_query_without_year(self):
        drive = object.__new__(GoogleDrive)
        drive.max_titles = 60
        query = drive.get_query(FakeMeta(year=None))
        self.assertEqual(len(query), 1)
        self.assertNotIn("None", query[0])

    def test_series_query_with_bad_season_does_not_crash(self):
        drive = object.__new__(GoogleDrive)
        drive.max_titles = 60
        meta = FakeMeta(stream_type="series")
        meta.se, meta.ep = "abc", "??"
        self.assertTrue(drive.get_query(meta))


class MetaTests(unittest.TestCase):
    def _parse(self, stream_id, stream_type="series"):
        def fake_init(self):
            self.titles = ["Some Show"]
            self.year = "2019"
            self.fetch_dest = "TEST"

        with patch.object(IMDb, "__init__", fake_init):
            return Meta(stream_type, stream_id)

    def test_encoded_separator(self):
        meta = self._parse("tt1234567%3A2%3A5")
        self.assertEqual(meta.id, "tt1234567")
        self.assertEqual((meta.se, meta.ep), ("02", "05"))

    def test_decoded_separator(self):
        meta = self._parse("tt1234567:3:7")
        self.assertEqual(meta.id, "tt1234567")
        self.assertEqual((meta.se, meta.ep), ("03", "07"))


class StreamTests(unittest.TestCase):
    def test_missing_fields_do_not_crash(self):
        drive = FakeDrive([{"id": "abc", "name": "Test.Movie.2020.mkv", "size": None}])
        streams = Streams(drive, FakeMeta(titles=["test movie"], year="2020"))
        self.assertEqual(len(streams.results), 1)
        self.assertIn("0.00B", streams.results[0]["title"])

    def test_missing_drive_name_is_handled(self):
        drive = FakeDrive([{
            "id": "abc",
            "name": "Test.Movie.2020.1080p.mkv",
            "size": "1000",
            "driveId": "missing-drive",
        }])
        streams = Streams(drive, FakeMeta(titles=["test movie"], year="2020"))
        self.assertEqual(len(streams.results), 1)
        self.assertIn("MyDrive", streams.results[0]["title"])

    def test_best_res_does_not_mutate_stream(self):
        drive = FakeDrive([])
        streams = Streams(drive, FakeMeta())
        item = {"sortkeys": {"res": "1080p", "title": "test movie", "se": "x"}}
        streams.best_res(item)
        self.assertIn("sortkeys", item)


class RouteTests(unittest.TestCase):
    def test_error_still_produces_valid_json(self):
        with patch("sgd.routes.Meta", side_effect=RuntimeError("boom")):
            body = "".join(get_streams("movie", "tt1234567"))
        self.assertEqual(json.loads(body), {"streams": []})


class CacheAndUtilsTests(unittest.TestCase):
    def test_corrupt_json_cache_is_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not valid json")
            with patch("sgd.cache.CACHE_DIR", directory):
                cache = Json("cache.json")
            self.assertEqual(cache.contents, {})

    def test_hr_size_handles_none(self):
        self.assertEqual(hr_size(None), "0.00B")

    def test_req_api_handles_callback_wrappers(self):
        with patch("sgd.utils.req_wrapper", return_value='callback({"meta": {"name": "X"}})'):
            self.assertEqual(req_api("anything"), {"name": "X"})


if __name__ == "__main__":
    unittest.main()
