import unittest

from plugins.calibre_umcp_plugin.bridge import BridgeMethodError, CalibreRpcBridge


class FakeMetadata:
    def __init__(self, title, authors, identifiers=None, tags=None):
        self.title = title
        self.authors = authors
        self.identifiers = identifiers or {}
        self.tags = tags or []
        self.series = None
        self.series_index = None
        self.publisher = None


class FakeDb:
    library_path = "/books"

    def __init__(self):
        self.rows = {
            1: FakeMetadata("Example", ["Author"], {"isbn": "1"}),
            2: FakeMetadata("Example", ["Author"], {"isbn": "1"}),
            3: FakeMetadata("Other", ["Someone"]),
        }

    def all_book_ids(self):
        return list(self.rows)

    def search_getting_ids(self, query, _):
        return [book_id for book_id, meta in self.rows.items() if query.casefold() in meta.title.casefold()]

    def get_metadata(self, book_id, index_is_id=True):
        self.assert_index_is_id(index_is_id)
        return self.rows[book_id]

    def formats(self, book_id, index_is_id=True):
        self.assert_index_is_id(index_is_id)
        return "EPUB,PDF" if book_id == 1 else "EPUB"

    @staticmethod
    def assert_index_is_id(index_is_id):
        if index_is_id is not True:
            raise AssertionError("index_is_id must be True")


class FakeGui:
    current_db = FakeDb()


class CalibreRpcBridgeTests(unittest.TestCase):
    def test_bridge_searches_current_library(self):
        bridge = CalibreRpcBridge(FakeGui())
        rows = bridge.dispatch("search_books", {"query": "Example", "limit": 10})
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["library_path"], "/books")
        self.assertEqual(rows[0]["formats"], ["EPUB", "PDF"])

    def test_bridge_finds_duplicates(self):
        bridge = CalibreRpcBridge(FakeGui())
        duplicates = bridge.dispatch("find_duplicates", {"limit": 10})
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["count"], 2)

    def test_mutations_are_rejected_but_audited_until_safe_api_mapping_exists(self):
        bridge = CalibreRpcBridge(FakeGui())
        with self.assertRaises(BridgeMethodError) as caught:
            bridge.dispatch("move_book", {"book_id": 1})
        self.assertIn("job APIs", str(caught.exception))
        jobs = bridge.dispatch("list_jobs", {})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["method"], "move_book")
        self.assertEqual(jobs[0]["status"], "rejected")
        self.assertEqual(bridge.dispatch("get_job_status", {"job_id": jobs[0]["id"]}), jobs[0])


if __name__ == "__main__":
    unittest.main()
