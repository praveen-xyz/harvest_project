"""
=============================================================================
  Web Image Extractor — Test Suite
  File    : test_app.py
  Run     : python -m pytest test_app.py -v
  Requires: pytest  (pip install pytest)
=============================================================================

Tests are organised into four groups:
  1. URL Validation tests
  2. Image Extraction / HTML parsing tests
  3. Download helper tests  (uses a local mock HTTP server)
  4. API route tests        (Flask test client)
"""

import io
import os
import sys
import json
import shutil
import threading
import tempfile
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Make sure app.py can be imported from the same directory
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

import app as backend
from app import (
    validate_url,
    normalise_url,
    extract_srcset_urls,
    extract_images,
    safe_filename,
    app as flask_app,
)


# =============================================================================
# 1. URL VALIDATION TESTS
# =============================================================================

class TestURLValidation(unittest.TestCase):
    """Tests for the validate_url() helper."""

    # --- Valid URLs ---

    def test_valid_http_url(self):
        ok, reason = validate_url("http://example.com")
        self.assertTrue(ok, reason)

    def test_valid_https_url(self):
        ok, reason = validate_url("https://example.com/page?q=1")
        self.assertTrue(ok, reason)

    def test_valid_url_with_path(self):
        ok, reason = validate_url("https://www.wikipedia.org/wiki/Python")
        self.assertTrue(ok, reason)

    # --- Rejected by scheme ---

    def test_empty_url_rejected(self):
        ok, reason = validate_url("")
        self.assertFalse(ok)
        self.assertIn("required", reason.lower())

    def test_ftp_url_rejected(self):
        ok, reason = validate_url("ftp://example.com/file.jpg")
        self.assertFalse(ok)

    def test_file_url_rejected(self):
        ok, reason = validate_url("file:///etc/passwd")
        self.assertFalse(ok)
        # The validator rejects file:// at the scheme check — either
        # the dedicated file:// message or the generic http/https message
        self.assertTrue("file://" in reason or "http" in reason.lower())

    def test_no_scheme_rejected(self):
        ok, reason = validate_url("example.com")
        self.assertFalse(ok)

    # --- Rejected by host ---

    def test_localhost_rejected(self):
        ok, reason = validate_url("http://localhost/admin")
        self.assertFalse(ok)
        self.assertIn("localhost", reason.lower())

    def test_loopback_ip_rejected(self):
        ok, reason = validate_url("http://127.0.0.1/")
        self.assertFalse(ok)

    def test_missing_hostname_rejected(self):
        ok, reason = validate_url("https:///path")
        self.assertFalse(ok)

    # --- Private IP detection ---

    def test_private_ip_detection(self):
        """is_private_ip() should return True for RFC-1918 addresses."""
        self.assertTrue(backend.is_private_ip("192.168.1.1"))
        self.assertTrue(backend.is_private_ip("10.0.0.1"))
        self.assertTrue(backend.is_private_ip("172.16.5.5"))
        self.assertTrue(backend.is_private_ip("127.0.0.1"))

    def test_public_ip_not_private(self):
        """is_private_ip() should return False for public IPs."""
        # 8.8.8.8 is Google DNS — clearly public
        result = backend.is_private_ip("8.8.8.8")
        self.assertFalse(result)


# =============================================================================
# 2. IMAGE EXTRACTION / HTML PARSING TESTS
# =============================================================================

class TestNormaliseURL(unittest.TestCase):
    """Tests for the normalise_url() helper."""

    BASE = "https://example.com/page/"

    def test_absolute_url_unchanged(self):
        url = "https://cdn.example.com/img.jpg"
        self.assertEqual(normalise_url(url, self.BASE), url)

    def test_relative_url_resolved(self):
        result = normalise_url("/images/logo.png", self.BASE)
        self.assertEqual(result, "https://example.com/images/logo.png")

    def test_relative_path_resolved(self):
        result = normalise_url("../img/photo.jpg", self.BASE)
        self.assertIn("photo.jpg", result)

    def test_protocol_relative_url(self):
        result = normalise_url("//cdn.example.com/img.png", self.BASE)
        self.assertTrue(result.startswith("https://"))

    def test_data_uri_returns_none(self):
        result = normalise_url("data:image/png;base64,abc123", self.BASE)
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = normalise_url("", self.BASE)
        self.assertIsNone(result)

    def test_none_returns_none(self):
        result = normalise_url(None, self.BASE)
        self.assertIsNone(result)


class TestExtractSrcset(unittest.TestCase):
    """Tests for the extract_srcset_urls() helper."""

    BASE = "https://example.com/"

    def test_single_entry(self):
        srcset = "https://example.com/img-320.jpg 320w"
        result = extract_srcset_urls(srcset, self.BASE)
        self.assertEqual(len(result), 1)
        self.assertIn("img-320.jpg", result[0])

    def test_multiple_entries(self):
        srcset = "img-320.jpg 320w, img-640.jpg 640w, img-1280.jpg 2x"
        result = extract_srcset_urls(srcset, self.BASE)
        self.assertEqual(len(result), 3)

    def test_empty_srcset(self):
        result = extract_srcset_urls("", self.BASE)
        self.assertEqual(result, [])

    def test_no_descriptor(self):
        srcset = "https://example.com/img.jpg"
        result = extract_srcset_urls(srcset, self.BASE)
        self.assertEqual(len(result), 1)


class TestExtractImages(unittest.TestCase):
    """Tests for the extract_images() function that parses full HTML."""

    BASE = "https://example.com/"

    def _run(self, html: str) -> list:
        return extract_images(html, self.BASE)

    def test_basic_img_src(self):
        html = '<html><body><img src="https://example.com/photo.jpg"></body></html>'
        imgs = self._run(html)
        self.assertIn("https://example.com/photo.jpg", imgs)

    def test_img_data_src(self):
        html = '<html><body><img data-src="https://cdn.example.com/lazy.jpg"></body></html>'
        imgs = self._run(html)
        self.assertIn("https://cdn.example.com/lazy.jpg", imgs)

    def test_img_data_lazy_src(self):
        html = '<html><body><img data-lazy-src="https://example.com/slow.jpg"></body></html>'
        imgs = self._run(html)
        self.assertIn("https://example.com/slow.jpg", imgs)

    def test_opengraph_image(self):
        html = (
            '<html><head>'
            '<meta property="og:image" content="https://example.com/og.jpg">'
            '</head><body></body></html>'
        )
        imgs = self._run(html)
        self.assertIn("https://example.com/og.jpg", imgs)

    def test_twitter_image(self):
        html = (
            '<html><head>'
            '<meta name="twitter:image" content="https://example.com/tw.jpg">'
            '</head><body></body></html>'
        )
        imgs = self._run(html)
        self.assertIn("https://example.com/tw.jpg", imgs)

    def test_srcset_parsed(self):
        html = (
            '<html><body>'
            '<img srcset="https://example.com/sm.jpg 320w, '
            'https://example.com/lg.jpg 1280w">'
            '</body></html>'
        )
        imgs = self._run(html)
        self.assertIn("https://example.com/sm.jpg", imgs)
        self.assertIn("https://example.com/lg.jpg", imgs)

    def test_deduplication(self):
        html = (
            '<html><body>'
            '<img src="https://example.com/same.jpg">'
            '<img src="https://example.com/same.jpg">'
            '</body></html>'
        )
        imgs = self._run(html)
        self.assertEqual(imgs.count("https://example.com/same.jpg"), 1)

    def test_data_uri_excluded(self):
        html = '<html><body><img src="data:image/png;base64,abc"></body></html>'
        imgs = self._run(html)
        self.assertEqual(len(imgs), 0)

    def test_relative_url_resolved(self):
        html = '<html><body><img src="/assets/img.png"></body></html>'
        imgs = self._run(html)
        self.assertIn("https://example.com/assets/img.png", imgs)

    def test_max_images_limit(self):
        # Generate more images than MAX_IMAGES
        many_imgs = "".join(
            f'<img src="https://example.com/img{i}.jpg">'
            for i in range(backend.MAX_IMAGES + 50)
        )
        html = f"<html><body>{many_imgs}</body></html>"
        imgs = self._run(html)
        self.assertLessEqual(len(imgs), backend.MAX_IMAGES)

    def test_empty_html(self):
        imgs = self._run("")
        self.assertEqual(imgs, [])

    def test_picture_source_srcset(self):
        html = (
            '<html><body><picture>'
            '<source srcset="https://example.com/wide.jpg 1200w">'
            '<img src="https://example.com/default.jpg">'
            '</picture></body></html>'
        )
        imgs = self._run(html)
        self.assertIn("https://example.com/wide.jpg", imgs)


# =============================================================================
# 3. DOWNLOAD HELPER TESTS
# =============================================================================

class TestSafeFilename(unittest.TestCase):
    """Tests for the safe_filename() helper."""

    def test_normal_filename(self):
        name = safe_filename("https://example.com/photo.jpg", set())
        self.assertTrue(name.endswith(".jpg"))
        self.assertNotIn("/", name)

    def test_special_chars_stripped(self):
        name = safe_filename("https://example.com/my photo!@#.jpg", set())
        self.assertNotIn(" ", name)
        self.assertNotIn("!", name)

    def test_collision_gets_hash(self):
        existing = {"photo.jpg"}
        name = safe_filename("https://example.com/photo.jpg", existing)
        # Should be different since "photo.jpg" is already taken
        self.assertNotEqual(name, "photo.jpg")

    def test_no_extension_gets_one(self):
        name = safe_filename("https://example.com/imagewithnoext", set())
        self.assertIn(".", name)

    def test_empty_path_gets_image_name(self):
        name = safe_filename("https://example.com/", set())
        self.assertTrue(len(name) > 0)


class TestDownloadSingleImage(unittest.TestCase):
    """Tests for download_single_image() using a real local HTTP server."""

    @classmethod
    def setUpClass(cls):
        """Start a tiny local HTTP server that serves a fake JPEG."""

        class FakeImageHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ok.jpg":
                    # Minimal valid JPEG header
                    data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/big.jpg":
                    self.send_response(200)
                    self.send_header("Content-Length",
                                     str((backend.MAX_FILE_SIZE_MB + 1) * 1024 * 1024))
                    self.end_headers()
                elif self.path == "/notfound.jpg":
                    self.send_response(404)
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass  # Suppress server output during tests

        cls.server = HTTPServer(("127.0.0.1", 0), FakeImageHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.tmpdir = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_successful_download(self):
        result = backend.download_single_image(self._url("/ok.jpg"), self.tmpdir, set())
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.tmpdir / result["filename"]).exists())

    def test_404_gives_error(self):
        result = backend.download_single_image(self._url("/notfound.jpg"), self.tmpdir, set())
        self.assertEqual(result["status"], "error")
        self.assertIn("404", result["reason"])

    def test_large_file_skipped_by_header(self):
        result = backend.download_single_image(self._url("/big.jpg"), self.tmpdir, set())
        self.assertEqual(result["status"], "skipped")


# =============================================================================
# 4. API ROUTE TESTS  (Flask test client)
# =============================================================================

class TestHealthRoute(unittest.TestCase):
    """Tests for GET /health."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_ok_status(self):
        resp = self.client.get("/health")
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "ok")

    def test_health_has_timestamp(self):
        resp = self.client.get("/health")
        data = json.loads(resp.data)
        self.assertIn("timestamp", data)
        self.assertIsInstance(data["timestamp"], int)


class TestExtractRoute(unittest.TestCase):
    """Tests for POST /extract."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def _post(self, payload):
        return self.client.post(
            "/extract",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_missing_url_returns_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data["success"])

    def test_localhost_url_rejected(self):
        resp = self._post({"url": "http://localhost/secret"})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data["success"])

    def test_file_url_rejected(self):
        resp = self._post({"url": "file:///etc/passwd"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_scheme_rejected(self):
        resp = self._post({"url": "ftp://example.com"})
        self.assertEqual(resp.status_code, 400)

    def test_empty_url_rejected(self):
        resp = self._post({"url": ""})
        self.assertEqual(resp.status_code, 400)

    @patch("app.fetch_page")
    def test_successful_extraction(self, mock_fetch):
        """Mock fetch_page to avoid real HTTP requests in tests."""
        mock_fetch.return_value = (
            '<html><body>'
            '<img src="https://example.com/a.jpg">'
            '<img src="https://example.com/b.png">'
            '</body></html>',
            "https://example.com/",
        )
        resp = self._post({"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["count"], 2)
        self.assertIn("https://example.com/a.jpg", data["images"])

    @patch("app.fetch_page")
    def test_fetch_failure_returns_502(self, mock_fetch):
        """When fetch_page fails, the API should return 502."""
        mock_fetch.return_value = (None, "Connection refused")
        resp = self._post({"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 502)
        data = json.loads(resp.data)
        self.assertFalse(data["success"])


class TestDownloadRoute(unittest.TestCase):
    """Tests for POST /download."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

    def _post(self, payload):
        return self.client.post(
            "/download",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_missing_images_key_returns_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)

    def test_empty_images_list_returns_400(self):
        resp = self._post({"images": []})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_urls_rejected(self):
        resp = self._post({"images": ["not-a-url", "ftp://bad.com"]})
        self.assertEqual(resp.status_code, 400)

    def test_too_many_images_returns_400(self):
        urls = [f"https://example.com/img{i}.jpg" for i in range(backend.MAX_IMAGES + 1)]
        resp = self._post({"images": urls})
        self.assertEqual(resp.status_code, 400)

    @patch("app.download_images_concurrent")
    def test_successful_download_response(self, mock_dl):
        """Mock concurrent downloader to avoid real HTTP requests."""
        mock_dl.return_value = [
            {"url": "https://example.com/a.jpg", "status": "ok",
             "filename": "a.jpg", "reason": "10 KB saved"},
        ]
        resp = self._post({"images": ["https://example.com/a.jpg"]})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["saved"], 1)
        self.assertEqual(data["errors"], 0)


# =============================================================================
# RUN
# =============================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
