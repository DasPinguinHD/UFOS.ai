"""Regression (live test 2026-10-18): aiohttp's MultiDictProxy.getall(key) raises KeyError for a
missing key unless a default is passed, so every optional query parameter of /research returned 500.
The fake below mimics that aiohttp/multidict behaviour (no aiohttp needed in the test environment)."""
import unittest

from market_research.data_api import BadParam, _one


class _MultiDictLike:
    def __init__(self, pairs):
        self._pairs = list(pairs)

    _NO = object()

    def getall(self, key, default=_NO):
        vals = [v for k, v in self._pairs if k == key]
        if not vals:
            if default is self._NO:
                raise KeyError(key)
            return default
        return vals

    def __iter__(self):
        return iter(k for k, _ in self._pairs)

    def __contains__(self, key):
        return any(k == key for k, _ in self._pairs)


class QueryGetallTests(unittest.TestCase):
    def test_present(self):
        self.assertEqual(_one(_MultiDictLike([("q", "AAPL")]), "q"), "AAPL")

    def test_missing_optional_is_none(self):
        self.assertIsNone(_one(_MultiDictLike([("q", "AAPL")]), "before"))

    def test_duplicate_refused(self):
        with self.assertRaises(BadParam):
            _one(_MultiDictLike([("a", "1"), ("a", "2")]), "a")

    def test_plain_dict(self):
        self.assertIsNone(_one({}, "before"))
        self.assertEqual(_one({"tf": "1D"}, "tf"), "1D")


if __name__ == "__main__":
    unittest.main()
