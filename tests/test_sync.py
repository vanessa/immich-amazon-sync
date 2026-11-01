"""End-to-end tests for immich_sync.main() with all HTTP mocked at urlopen."""

import gzip
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import immich_sync  # noqa: E402

IMMICH = "http://immich.test:2283"
DRIVE = "https://www.amazon.de/drive/v1"
CONTENT = "https://content-eu.drive.amazonaws.com/cdproxy"

SECRETS = {
    "IMMICH_API_KEY": "immich-secret-key-123",
    "AP_SESSION_ID": "262-1111111-2222222",
    "AP_UBID_VALUE": "ubid-secret-333",
    "AP_AT_VALUE": "Atza|at-secret-444",
}


def aid(n: int) -> str:
    return str(uuid.UUID(int=n, version=4))


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        super().__init__(payload)
        self.headers = headers or {"Content-Length": str(len(payload))}


class Backend:
    """In-memory Immich 3.x + Amazon Drive used as the urlopen replacement."""

    def __init__(self):
        self.immich_version = {"major": 3, "minor": 3, "patch": 1, "prerelease": None}
        self.albums = {}  # immich album id -> {"name", "assets": [asset dicts], "count": override}
        self.page_size = None  # force small search pages
        self.legacy_search = False
        self.search_fails = False
        self.amazon_status = None  # force every Amazon call to fail with this code
        self.upload_conflict = {}  # name -> existing node id
        self.nodes = {"root": {"id": "root", "kind": "FOLDER", "isRoot": True, "status": "AVAILABLE"}}
        self.album_children = {}  # album node id -> set(node ids)
        self.trashed = set()
        self.trash_rejects = set()  # nodes for which PATCH /trash fails with 400
        self.gzip_children = False  # answer PATCH .../children gzip-compressed, as amazon.de does
        self.calls = []  # (method, url, body)
        self._n = 0

    # Immich fixtures

    def add_album(self, name, asset_ids, **asset_overrides):
        album_id = aid(10_000 + len(self.albums))
        assets = [self.make_asset(a, **asset_overrides) for a in asset_ids]
        self.albums[album_id] = {"name": name, "assets": assets, "count": None}
        return album_id

    @staticmethod
    def make_asset(asset_id, **overrides):
        asset = {
            "id": asset_id,
            "originalFileName": f"IMG_{asset_id[-4:]}.jpg",
            "originalMimeType": "image/jpeg",
            "type": "IMAGE",
            "isTrashed": False,
            "visibility": "timeline",
        }
        asset.update(overrides)
        return asset

    # routing

    def __call__(self, req, timeout=None):
        url = req.full_url
        method = req.get_method()
        data = req.data
        if data is not None and not isinstance(data, bytes):
            data = data.read()
        self.calls.append((method, url, data))
        if url.startswith(IMMICH):
            return self.immich(method, url, data)
        if url.startswith("https://hc.test/"):
            return FakeResponse(b"OK")
        if self.amazon_status:
            raise HTTPError(url, self.amazon_status, "err", {}, io.BytesIO(b'{"message":"denied"}'))
        if url.startswith(CONTENT):
            return self.upload(data)
        if url.startswith(DRIVE):
            return self.drive(method, url, data)
        raise AssertionError(f"unexpected URL {url}")

    def immich(self, method, url, data):
        path = urlsplit(url).path[len("/api/"):]
        if path == "server/version":
            return FakeResponse(self.immich_version)
        if path == "albums":
            return FakeResponse([{"id": k, "albumName": v["name"]} for k, v in self.albums.items()])
        if path.startswith("albums/"):
            album = self.albums[path.split("/")[1]]
            count = album["count"] if album["count"] is not None else len(
                [a for a in album["assets"] if not a["isTrashed"]])
            return FakeResponse({"id": path.split("/")[1], "assetCount": count})
        if path == "search/metadata":
            if self.search_fails:
                raise HTTPError(url, 500, "boom", {}, io.BytesIO(b"{}"))
            body = json.loads(data)
            if self.legacy_search:
                return FakeResponse({"albums": {}, "assets": {"items": [], "nextPage": None}})
            assert "albumIds" not in body and "page" not in body, "deprecated flat fields used"
            album_id = body["filter"]["albumIds"]["any"][0]
            assert body["filter"]["trashedAt"] == {"eq": None}
            assets = [a for a in self.albums[album_id]["assets"] if not a["isTrashed"]]
            size = self.page_size or body["size"]
            offset = int(body.get("cursor", 0))
            page = assets[offset:offset + size]
            nxt = str(offset + size) if offset + size < len(assets) else None
            return FakeResponse({"albums": {}, "assets": {
                "items": page, "count": len(page), "nextCursor": nxt, "nextPage": None}})
        if path.startswith("assets/") and path.endswith("/original"):
            return FakeResponse(f"content-of-{path.split('/')[1]}".encode())
        raise AssertionError(f"unexpected Immich path {path}")

    def upload(self, body):
        meta = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
        if meta["name"] in self.upload_conflict:
            raise HTTPError("u", 409, "conflict", {}, io.BytesIO(json.dumps(
                {"code": "NAME_ALREADY_EXISTS", "info": {"nodeId": self.upload_conflict[meta["name"]]}}
            ).encode()))
        self._n += 1
        node_id = f"node{self._n}"
        self.nodes[node_id] = {"id": node_id, "name": meta["name"], "kind": "FILE",
                               "status": "AVAILABLE", "parents": meta["parents"]}
        return FakeResponse({"id": node_id})

    def drive(self, method, url, data):
        parts = urlsplit(url)
        path = parts.path[len("/drive/v1/"):]
        query = parse_qs(parts.query)
        body = json.loads(data) if data else None
        if method == "GET" and path == "nodes":
            filters = unquote(query["filters"][0])
            if filters == "isRoot:true":
                return FakeResponse({"count": 1, "data": [self.nodes["root"]]})
            if filters == "kind:VISUAL_COLLECTION":
                found = [n for n in self.nodes.values() if n["kind"] == "VISUAL_COLLECTION"]
                return FakeResponse({"count": len(found), "data": found})
            return FakeResponse({"count": 0, "data": []})
        if method == "GET" and path.startswith("nodes/"):
            node = self.nodes.get(path.split("/")[1])
            if not node:
                raise HTTPError(url, 404, "nf", {}, io.BytesIO(b"{}"))
            return FakeResponse(node)
        if method == "POST" and path == "nodes":
            assert body["kind"] == "VISUAL_COLLECTION"
            node = {"id": f"album{len(self.album_children) + 1}", "name": body["name"],
                    "kind": "VISUAL_COLLECTION", "status": "AVAILABLE"}
            self.nodes[node["id"]] = node
            self.album_children[node["id"]] = set()
            return FakeResponse(node)
        if method == "PATCH" and path.endswith("/children"):
            children = self.album_children[path.split("/")[1]]
            if body["op"] == "add":
                children.update(body["value"])
            else:
                children.difference_update(body["value"])
            if self.gzip_children:
                raw = gzip.compress(json.dumps({"op": body["op"]}).encode())
                return FakeResponse(raw, {"Content-Length": str(len(raw)), "Content-Encoding": "gzip"})
            return FakeResponse({})
        if method == "PATCH" and path == "trash":
            assert body["op"] == "add"
            if self.trash_rejects & set(body["value"]):
                raise HTTPError(url, 400, "bad", {}, io.BytesIO(b'{"message":"already trashed"}'))
            self.trashed.update(body["value"])
            return FakeResponse({})
        raise AssertionError(f"unexpected Amazon call {method} {path}")

    def amazon_writes(self):
        return [(m, u) for m, u, _ in self.calls
                if not u.startswith(IMMICH) and m in ("POST", "PATCH", "PUT", "DELETE")]


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.backend = Backend()
        self.env = {
            "IMMICH_URL": IMMICH,
            "IMMICH_ALBUMS": "Echo",
            "AP_ALBUM_NAME": "Echo Show",
            "STATE_DIR": str(self.state_dir),
            **SECRETS,
        }

    def tearDown(self):
        immich_sync._teardown_logging()
        self.tmp.cleanup()

    def run_sync(self, **env):
        with patch.dict(os.environ, {**self.env, **env}, clear=True), \
                patch("immich_sync.urlopen", self.backend), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            code = immich_sync.main()
        self.output = out.getvalue() + err.getvalue()
        return code

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def write_state(self, data):
        (self.state_dir / "state.json").write_text(json.dumps(data))


class TestImmichSearch(SyncTestCase):
    def test_pagination_follows_next_cursor(self):
        ids = [aid(i) for i in range(1, 8)]
        album = self.backend.add_album("Echo", ids)
        self.backend.page_size = 3
        with patch.dict(os.environ, self.env, clear=True):
            immich_sync._configure()
        with patch("immich_sync.urlopen", self.backend):
            assets = immich_sync.search_album_assets(album)
        self.assertEqual([a["id"] for a in assets], ids)
        bodies = [json.loads(d) for m, u, d in self.backend.calls if u.endswith("/search/metadata")]
        self.assertEqual(len(bodies), 3)
        self.assertNotIn("cursor", bodies[0])
        self.assertEqual([b.get("cursor") for b in bodies[1:]], ["3", "6"])
        self.assertEqual(bodies[0]["filter"]["visibility"], {"in": ["archive", "timeline"]})

    def test_old_immich_aborts(self):
        self.backend.immich_version = {"major": 3, "minor": 1, "patch": 0}
        self.backend.add_album("Echo", [aid(1)])
        self.assertEqual(self.run_sync(), immich_sync.EXIT_ERROR)
        self.assertIn("too old", self.output)
        self.assertEqual(self.backend.amazon_writes(), [])

    def test_missing_next_cursor_aborts(self):
        self.backend.legacy_search = True
        self.backend.add_album("Echo", [aid(1)])
        self.write_state({"synced": {aid(1): "nodeX"}})
        self.assertEqual(self.run_sync(), immich_sync.EXIT_ERROR)
        self.assertIn("nextCursor", self.output)
        self.assertEqual(self.backend.trashed, set())


class TestFullSync(SyncTestCase):
    def test_gzip_reply_to_album_add_counts_as_success(self):
        ids = [aid(i) for i in range(1, 4)]
        self.backend.add_album("Echo", ids)
        self.backend.gzip_children = True
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        state = self.state()
        self.assertEqual(state["pending_album_add"], [])
        self.assertEqual(self.backend.album_children[state["album_node_id"]], set(state["synced"].values()))
        self.assertNotIn("codec", self.output)

    def test_upload_into_album_then_idempotent(self):
        ids = [aid(i) for i in range(1, 4)]
        self.backend.add_album("Echo", ids)

        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        state = self.state()
        self.assertEqual(state["version"], 2)
        self.assertEqual(set(state["synced"]), set(ids))
        album_id = state["album_node_id"]
        self.assertEqual(self.backend.nodes[album_id]["name"], "Echo Show")
        self.assertEqual(self.backend.album_children[album_id], set(state["synced"].values()))
        self.assertEqual(state["pending_album_add"], [])
        # one batched "add" call for all three uploads
        adds = [c for c in self.backend.calls if c[0] == "PATCH" and c[1].endswith("/children")]
        self.assertEqual(len(adds), 1)
        if os.name != "nt":
            for name in ("state.json", "immich-sync.log"):
                self.assertEqual(os.stat(self.state_dir / name).st_mode & 0o077, 0, name)

        # second run: nothing to do
        self.backend.calls.clear()
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertEqual(self.backend.amazon_writes(), [])
        self.assertIn("Uploaded: 0, Deleted: 0", self.output)

    def test_removed_photo_leaves_album_and_is_trashed(self):
        ids = [aid(i) for i in range(1, 7)]
        album = self.backend.add_album("Echo", ids)
        self.run_sync()
        node = self.state()["synced"][ids[0]]
        album_node = self.state()["album_node_id"]

        self.backend.albums[album]["assets"].pop(0)
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertNotIn(ids[0], self.state()["synced"])
        self.assertNotIn(node, self.backend.album_children[album_node])
        self.assertEqual(self.backend.trashed, {node})

    def test_album_only_mode_does_not_trash(self):
        ids = [aid(i) for i in range(1, 7)]
        album = self.backend.add_album("Echo", ids)
        self.run_sync()
        self.backend.albums[album]["assets"].pop(0)
        self.assertEqual(self.run_sync(AP_DELETE_MODE="album_only"), immich_sync.EXIT_OK)
        self.assertEqual(self.backend.trashed, set())
        self.assertNotIn(ids[0], self.state()["synced"])

    def test_trashed_and_filtered_types_are_not_uploaded(self):
        album = self.backend.add_album("Echo", [aid(1)])
        self.backend.albums[album]["assets"] += [
            Backend.make_asset(aid(2), isTrashed=True),
            Backend.make_asset(aid(3), type="VIDEO", originalFileName="clip.mp4"),
        ]
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertEqual(set(self.state()["synced"]), {aid(1)})

        self.assertEqual(self.run_sync(SYNC_TYPES="IMAGE,VIDEO"), immich_sync.EXIT_OK)
        self.assertEqual(set(self.state()["synced"]), {aid(1), aid(3)})

    def test_existing_album_is_reused_and_migrated_state_is_linked(self):
        self.backend.nodes["albX"] = {"id": "albX", "name": "Echo Show",
                                      "kind": "VISUAL_COLLECTION", "status": "AVAILABLE"}
        self.backend.album_children["albX"] = set()
        self.backend.nodes["old1"] = {"id": "old1", "kind": "FILE", "status": "AVAILABLE"}
        self.backend.add_album("Echo", [aid(1), aid(2)])
        self.write_state({"synced": {aid(1): "old1"}})  # original upstream format

        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        state = self.state()
        self.assertEqual(state["album_node_id"], "albX")
        self.assertEqual(state["synced"][aid(1)], "old1")  # not re-uploaded
        self.assertIn("old1", self.backend.album_children["albX"])
        uploads = [c for c in self.backend.calls if c[1].startswith(CONTENT)]
        self.assertEqual(len(uploads), 1)

    def test_conflict_adopts_node_with_same_md5(self):
        import hashlib

        self.backend.add_album("Echo", [aid(1)])
        name = Backend.make_asset(aid(1))["originalFileName"]
        md5 = hashlib.md5(f"content-of-{aid(1)}".encode()).hexdigest()
        self.backend.nodes["exist1"] = {"id": "exist1", "kind": "FILE", "status": "AVAILABLE",
                                        "contentProperties": {"md5": md5}}
        self.backend.upload_conflict[name] = "exist1"
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertEqual(self.state()["synced"][aid(1)], "exist1")

    def test_adopted_node_is_never_trashed(self):
        import hashlib

        album = self.backend.add_album("Echo", [aid(i) for i in range(1, 7)])
        name = Backend.make_asset(aid(1))["originalFileName"]
        md5 = hashlib.md5(f"content-of-{aid(1)}".encode()).hexdigest()
        # the user's own phone backup, somewhere else in the drive
        self.backend.nodes["backup"] = {"id": "backup", "kind": "FILE", "status": "AVAILABLE",
                                        "contentProperties": {"md5": md5}}
        self.backend.upload_conflict[name] = "backup"
        self.run_sync()
        self.assertEqual(self.state()["adopted_nodes"], ["backup"])
        album_node = self.state()["album_node_id"]

        self.backend.albums[album]["assets"].pop(0)
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertNotIn("backup", self.backend.trashed)
        self.assertNotIn("backup", self.backend.album_children[album_node])
        self.assertNotIn(aid(1), self.state()["synced"])
        self.assertEqual(self.state()["adopted_nodes"], [])

    def test_already_trashed_node_counts_as_deleted(self):
        album = self.backend.add_album("Echo", [aid(i) for i in range(1, 7)])
        self.run_sync()
        node = self.state()["synced"][aid(1)]
        # an interrupted earlier run already trashed it; Amazon now rejects the trash call
        self.backend.nodes[node]["status"] = "TRASH"
        self.backend.trash_rejects.add(node)
        self.backend.albums[album]["assets"].pop(0)
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertNotIn(aid(1), self.state()["synced"])

    def test_shared_node_is_not_trashed_while_still_used(self):
        album = self.backend.add_album("Echo", [aid(i) for i in range(1, 7)])
        self.backend.nodes["shared"] = {"id": "shared", "kind": "FILE", "status": "AVAILABLE"}
        self.write_state({"version": 2, "album_node_id": None, "pending_album_add": [],
                          "synced": {aid(1): "shared", aid(2): "shared"}})
        self.run_sync()
        self.backend.albums[album]["assets"].pop(0)  # aid(1) leaves, aid(2) stays
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.assertNotIn(aid(1), self.state()["synced"])
        self.assertEqual(self.state()["synced"][aid(2)], "shared")
        self.assertEqual(self.backend.trashed, set())

    def test_conflict_with_different_content_renames(self):
        self.backend.add_album("Echo", [aid(1)])
        name = Backend.make_asset(aid(1))["originalFileName"]
        self.backend.nodes["other"] = {"id": "other", "kind": "FILE", "status": "AVAILABLE",
                                       "contentProperties": {"md5": "0" * 32}}
        self.backend.upload_conflict[name] = "other"
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        node = self.state()["synced"][aid(1)]
        self.assertNotEqual(node, "other")
        self.assertEqual(self.backend.nodes[node]["name"], f"IMG_{aid(1)[-4:]}_{aid(1)[:8]}.jpg")

    def test_file_size_limit_skips(self):
        self.backend.add_album("Echo", [aid(1)])
        self.assertEqual(self.run_sync(MAX_FILE_MB="0.00001"), immich_sync.EXIT_OK)
        self.assertIn("Skipped", self.output)
        self.assertEqual(self.state()["synced"], {})

    def test_healthcheck_ping_only_on_success(self):
        self.backend.add_album("Echo", [aid(1)])
        self.run_sync(HEALTHCHECK_URL="https://hc.test/ping/abc")
        self.assertTrue(any(u.startswith("https://hc.test/") for _, u, _ in self.backend.calls))
        self.backend.calls.clear()
        self.backend.amazon_status = 401
        self.run_sync(HEALTHCHECK_URL="https://hc.test/ping/abc")
        self.assertFalse(any(u.startswith("https://hc.test/") for _, u, _ in self.backend.calls))


class TestDeletionSafety(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.ids = [aid(i) for i in range(1, 11)]
        self.album = self.backend.add_album("Echo", self.ids)
        self.assertEqual(self.run_sync(), immich_sync.EXIT_OK)
        self.before = self.state()
        self.backend.calls.clear()

    def assert_nothing_deleted(self):
        self.assertEqual(self.backend.trashed, set())
        self.assertLessEqual(self.before["synced"].items(), self.state()["synced"].items())
        removes = [c for c in self.backend.calls if c[0] == "PATCH" and b'"remove"' in (c[2] or b"")]
        self.assertEqual(removes, [])

    def test_asset_count_mismatch_blocks_deletions(self):
        self.backend.albums[self.album]["assets"].pop(0)
        self.backend.albums[self.album]["count"] = 10  # album claims more than search found
        self.assertEqual(self.run_sync(), immich_sync.EXIT_DELETE_BLOCKED)
        self.assertIn("assetCount=10", self.output)
        self.assert_nothing_deleted()

    def test_deletion_brake_fraction(self):
        del self.backend.albums[self.album]["assets"][:3]  # 30% > 20%
        self.assertEqual(self.run_sync(), immich_sync.EXIT_DELETE_BLOCKED)
        self.assertIn("Deletion brake", self.output)
        self.assert_nothing_deleted()

    def test_deletion_brake_count(self):
        del self.backend.albums[self.album]["assets"][:2]
        self.assertEqual(self.run_sync(MAX_DELETE_COUNT="1", MAX_DELETE_FRACTION="1"),
                         immich_sync.EXIT_DELETE_BLOCKED)
        self.assert_nothing_deleted()

    def test_empty_album_never_mass_deletes(self):
        self.backend.albums[self.album]["assets"] = []
        self.assertEqual(self.run_sync(), immich_sync.EXIT_DELETE_BLOCKED)
        self.assert_nothing_deleted()

    def test_missing_album_blocks_deletions(self):
        self.backend.add_album("Second", [aid(99)])
        self.backend.albums[self.album]["name"] = "Renamed"
        code = self.run_sync(IMMICH_ALBUMS="Echo,Second", MAX_DELETE_FRACTION="1",
                             MAX_DELETE_COUNT="100")
        self.assertEqual(code, immich_sync.EXIT_DELETE_BLOCKED)
        self.assertIn("Album not found", self.output)
        self.assert_nothing_deleted()

    def test_failed_immich_query_blocks_deletions(self):
        self.backend.search_fails = True
        self.backend.albums[self.album]["assets"] = []
        self.assertNotEqual(self.run_sync(MAX_DELETE_FRACTION="1", MAX_DELETE_COUNT="100"),
                            immich_sync.EXIT_OK)
        self.assert_nothing_deleted()

    def test_new_uploads_do_not_dilute_the_brake(self):
        # 3 of 10 tracked leave (30%) while 20 new photos arrive: 3/30 would pass 20%
        del self.backend.albums[self.album]["assets"][:3]
        self.backend.albums[self.album]["assets"] += [Backend.make_asset(aid(100 + i)) for i in range(20)]
        self.assertEqual(self.run_sync(), immich_sync.EXIT_DELETE_BLOCKED)
        self.assertIn("3 of 10 tracked", self.output)
        self.assertEqual(self.backend.trashed, set())

    def test_brake_can_be_lifted_explicitly(self):
        del self.backend.albums[self.album]["assets"][:3]
        self.assertEqual(self.run_sync(MAX_DELETE_FRACTION="0.5"), immich_sync.EXIT_OK)
        self.assertEqual(len(self.backend.trashed), 3)


class TestStreamingUploadOverSocket(unittest.TestCase):
    """The streamed multipart body must go over a real socket with Content-Length."""

    def test_upload_reaches_server_intact(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["headers"] = dict(self.headers)
                received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                payload = b'{"id": "n1"}'
                self.send_response(201)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "f"
                content = os.urandom(300_000)
                path.write_bytes(content)
                with patch("immich_sync.AMAZON_CONTENT_URL", f"http://127.0.0.1:{server.server_port}"), \
                        patch("immich_sync.AP_HEADERS", {"Cookie": "x=y"}):
                    node = immich_sync.upload_to_amazon(path, "IMG.jpg", "root")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(node, "n1")
        self.assertNotIn("Transfer-Encoding", received["headers"])
        self.assertIn(content, received["body"])
        self.assertIn(b'"parents": ["root"]', received["body"])
        self.assertIn(b'filename="IMG.jpg"', received["body"])


class TestRedirectNotFollowed(unittest.TestCase):
    def test_amazon_redirect_raises_auth_error_without_following(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/ap/signin")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch("immich_sync.AMAZON_DRIVE_URL", f"http://127.0.0.1:{server.server_port}/drive/v1"),                     patch("immich_sync.AP_HEADERS", {"Cookie": "x=y"}):
                with self.assertRaises(immich_sync.AmazonAuthError):
                    immich_sync.get_root_id()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].startswith("/ap/signin"))


class TestDryRun(SyncTestCase):
    def test_dry_run_writes_nothing(self):
        self.backend.add_album("Echo", [aid(1), aid(2)])
        self.assertEqual(self.run_sync(DRY_RUN="true"), immich_sync.EXIT_OK)
        self.assertEqual(self.backend.amazon_writes(), [])
        self.assertFalse((self.state_dir / "state.json").exists())
        self.assertIn("[dry-run] Would upload", self.output)
        self.assertIn("[dry-run] Would create Amazon Photos album", self.output)
        downloads = [u for _, u, _ in self.backend.calls if u.endswith("/original")]
        self.assertEqual(downloads, [])

    def test_dry_run_keeps_existing_state_and_reports_deletions(self):
        album = self.backend.add_album("Echo", [aid(i) for i in range(1, 7)])
        self.run_sync()
        before = (self.state_dir / "state.json").read_bytes()
        self.backend.albums[album]["assets"].pop(0)
        self.backend.calls.clear()
        self.assertEqual(self.run_sync(DRY_RUN="true"), immich_sync.EXIT_OK)
        self.assertEqual(self.backend.amazon_writes(), [])
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)
        self.assertIn("[dry-run] Would remove", self.output)


class TestAuthAndSecrets(SyncTestCase):
    def test_expired_cookies_abort_immediately(self):
        self.backend.add_album("Echo", [aid(i) for i in range(1, 6)])
        self.backend.amazon_status = 401
        self.assertEqual(self.run_sync(), immich_sync.EXIT_AUTH)
        self.assertIn("cookies expired", self.output)
        amazon_calls = [u for _, u, _ in self.backend.calls if not u.startswith(IMMICH)]
        self.assertEqual(len(amazon_calls), 1)  # aborted on the first request

    def test_forbidden_counts_as_auth_error(self):
        self.backend.add_album("Echo", [aid(1)])
        self.backend.amazon_status = 403
        self.assertEqual(self.run_sync(), immich_sync.EXIT_AUTH)

    def test_secrets_never_logged(self):
        self.backend.add_album("Echo", [aid(1)])
        self.backend.amazon_status = 401
        hc = "https://hc.test/ping/very-secret-token"
        self.run_sync(HEALTHCHECK_URL=hc)
        log_text = (self.state_dir / "immich-sync.log").read_text()
        for secret in [*SECRETS.values(), hc]:
            self.assertNotIn(secret, self.output)
            self.assertNotIn(secret, log_text)

    def test_redacting_formatter_masks_secrets_in_exceptions(self):
        import logging

        immich_sync._SECRETS.add("super-secret-value")
        try:
            raise RuntimeError("leaked super-secret-value here")
        except RuntimeError:
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "msg %s", ("super-secret-value",),
                                       sys.exc_info())
        text = immich_sync._RedactingFormatter().format(record)
        self.assertNotIn("super-secret-value", text)
        self.assertIn("***", text)

    def test_secrets_from_files(self):
        secret_file = self.state_dir / "at.txt"
        secret_file.write_text("from-file-secret\n")
        env = {k: v for k, v in self.env.items() if k != "AP_AT_VALUE"}
        env["AP_AT_VALUE_FILE"] = str(secret_file)
        with patch.dict(os.environ, env, clear=True):
            immich_sync._configure()
        self.assertIn("at-acbde=from-file-secret", immich_sync.AP_HEADERS["Cookie"])
        self.assertIn("ubid-acbde=", immich_sync.AP_HEADERS["Cookie"])

    def test_redirect_to_sign_in_means_expired_cookies(self):
        self.backend.add_album("Echo", [aid(1)])
        self.backend.amazon_status = 302
        self.assertEqual(self.run_sync(), immich_sync.EXIT_AUTH)

    def test_non_finite_numbers_rejected(self):
        for env in ({"MAX_DELETE_FRACTION": "nan"}, {"MAX_FILE_MB": "inf"}, {"MAX_DELETE_COUNT": "-1"}):
            self.assertEqual(self.run_sync(**env), immich_sync.EXIT_ERROR, env)
            self.assertIn("finite", self.output)

    def test_cookie_value_with_header_injection_rejected(self):
        self.assertEqual(self.run_sync(AP_AT_VALUE="abc\r\nX-Evil: 1"), immich_sync.EXIT_ERROR)

    def test_config_overrides(self):
        env = {**self.env, "AP_TLD": "com", "AP_UBID_KEY": "ubid-custom",
               "AP_CONTENT_URL": "https://upload.example/cdproxy/nodes", "AP_USER_AGENT": "UA/1"}
        with patch.dict(os.environ, env, clear=True):
            immich_sync._configure()
        self.assertEqual(immich_sync.AMAZON_DRIVE_URL, "https://www.amazon.com/drive/v1")
        self.assertEqual(immich_sync.AMAZON_CONTENT_URL, "https://upload.example/cdproxy")
        self.assertIn("ubid-custom=", immich_sync.AP_HEADERS["Cookie"])
        self.assertIn("at-main=", immich_sync.AP_HEADERS["Cookie"])
        self.assertEqual(immich_sync.AP_HEADERS["User-Agent"], "UA/1")


if __name__ == "__main__":
    unittest.main()
