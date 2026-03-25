"""Smoke tests for pure-logic helpers in immich_sync."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Allow importing from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestState(unittest.TestCase):
    """Test load_state / save_state round-trip."""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch("immich_sync.STATE_FILE", state_file):
                from immich_sync import load_state, save_state

                self.assertEqual(load_state(), {})

                data = {"asset1": "node1", "asset2": "node2"}
                save_state(data)
                self.assertEqual(load_state(), data)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "nonexistent" / "state.json"
            with patch("immich_sync.STATE_FILE", state_file):
                from immich_sync import load_state

                self.assertEqual(load_state(), {})


class TestEncodeMultipart(unittest.TestCase):
    """Test _encode_multipart produces valid multipart body."""

    def test_structure(self):
        from immich_sync import _encode_multipart

        body, content_type = _encode_multipart(
            {
                "field1": (None, b"value1", "text/plain"),
                "field2": ("file.txt", b"file content", "application/octet-stream"),
            }
        )

        self.assertIn("multipart/form-data; boundary=", content_type)
        boundary = content_type.split("boundary=")[1]

        self.assertIn(f"--{boundary}".encode(), body)
        self.assertIn(f"--{boundary}--".encode(), body)
        self.assertIn(b"value1", body)
        self.assertIn(b"file content", body)
        self.assertIn(b'name="field1"', body)
        self.assertIn(b'filename="file.txt"', body)

    def test_no_filename(self):
        from immich_sync import _encode_multipart

        body, _ = _encode_multipart(
            {"meta": (None, b'{"key": "val"}', "application/json")}
        )

        self.assertNotIn(b"filename=", body)
        self.assertIn(b'name="meta"', body)


class TestUrlWithParams(unittest.TestCase):
    """Test _url_with_params URL construction."""

    def test_base_only(self):
        from immich_sync import _url_with_params, AMAZON_BASE_PARAMS

        result = _url_with_params("https://example.com/path")
        self.assertEqual(result, f"https://example.com/path?{AMAZON_BASE_PARAMS}")

    def test_with_extra_params(self):
        from immich_sync import _url_with_params, AMAZON_BASE_PARAMS

        result = _url_with_params("https://example.com/path", "foo=bar")
        self.assertEqual(
            result, f"https://example.com/path?{AMAZON_BASE_PARAMS}&foo=bar"
        )


if __name__ == "__main__":
    unittest.main()
