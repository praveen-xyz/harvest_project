"""
Project Harvest — Automated Test Suite
Covers unit, integration, security, and edge-case tests as per SRS §13.

Run:  pytest test_app.py -v
"""

import os
import json
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

os.environ["HARVEST_DEV"] = "false"

from app import app, extract_images, validate_url, _safe_filename, _is_private_ip, IMAGES_DIR


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def clean_images():
    yield
    if IMAGES_DIR.exists():
        shutil.rmtree(IMAGES_DIR, ignore_errors=True)


# ===========================================================================
# UNIT TESTS — Image Extraction
# ===========================================================================

class TestExtraction:

    def test_img_src(self):
        html = '<html><body><img src="/photo.jpg" alt="A photo"></body></html>'
        result = extract_images(html, "https://example.com/page")
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/photo.jpg"
        assert result[0]["alt"] == "A photo"

    def test_img_data_src_lazy(self):
        html = '<img data-src="/lazy.png">'
        result = extract_images(html, "https://example.com")
        urls = [r["url"] for r in result]
        assert "https://example.com/lazy.png" in urls

    def test_srcset(self):
        html = '<img srcset="/small.jpg 300w, /large.jpg 800w">'
        result = extract_images(html, "https://example.com")
        urls = [r["url"] for r in result]
        assert "https://example.com/small.jpg" in urls
        assert "https://example.com/large.jpg" in urls

    def test_picture_source(self):
        html = '<picture><source srcset="/modern.webp" type="image/webp"><img src="/fallback.jpg"></picture>'
        result = extract_images(html, "https://example.com")
        urls = [r["url"] for r in result]
        assert "https://example.com/modern.webp" in urls
        assert "https://example.com/fallback.jpg" in urls

    def test_favicon(self):
        html = '<link rel="icon" href="/favicon.ico">'
        result = extract_images(html, "https://example.com")
        urls = [r["url"] for r in result]
        assert "https://example.com/favicon.ico" in urls

    def test_css_background(self):
        html = '<div style="background-image: url(\'/bg.png\')"></div>'
        result = extract_images(html, "https://example.com")
        urls = [r["url"] for r in result]
        assert "https://example.com/bg.png" in urls

    def test_style_block_background(self):
        html = '<style>.hero { background-image: url("/hero.jpg"); }</style>'
        result = extract_images(html, "https://example.com")
        urls = [r["url"] for r in result]
        assert "https://example.com/hero.jpg" in urls

    def test_relative_url_resolution(self):
        html = '<img src="assets/pic.jpg">'
        result = extract_images(html, "https://example.com/pages/about.html")
        assert result[0]["url"] == "https://example.com/pages/assets/pic.jpg"

    def test_deduplication(self):
        html = '<img src="/dup.jpg"><img src="/dup.jpg">'
        result = extract_images(html, "https://example.com")
        assert len(result) == 1

    def test_data_uri_excluded(self):
        html = '<img src="data:image/png;base64,abc123">'
        result = extract_images(html, "https://example.com")
        assert len(result) == 0

    def test_no_images(self):
        html = '<html><body><p>No images here</p></body></html>'
        result = extract_images(html, "https://example.com")
        assert len(result) == 0


# ===========================================================================
# UNIT TESTS — URL Validation (SR-01)
# ===========================================================================

class TestURLValidation:

    def test_empty_url(self):
        with pytest.raises(ValueError, match="empty"):
            validate_url("")

    def test_scheme_prepending(self):
        with patch("app.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(2, 1, 0, '', ('93.184.216.34', 0))]
            result = validate_url("example.com")
            assert result == "https://example.com"

    def test_ftp_rejected(self):
        with pytest.raises(ValueError, match="http"):
            validate_url("ftp://example.com")

    def test_localhost_blocked(self):
        with pytest.raises(ValueError, match="private"):
            validate_url("http://localhost")

    def test_private_ip_blocked(self):
        with pytest.raises(ValueError, match="private"):
            validate_url("http://192.168.1.1")

    def test_loopback_blocked(self):
        with pytest.raises(ValueError, match="private"):
            validate_url("http://127.0.0.1")

    def test_link_local_blocked(self):
        with pytest.raises(ValueError, match="private"):
            validate_url("http://169.254.169.254")


class TestPrivateIP:
    def test_loopback(self):
        assert _is_private_ip("127.0.0.1") is True
    def test_private_10(self):
        assert _is_private_ip("10.0.0.1") is True
    def test_private_192(self):
        assert _is_private_ip("192.168.1.1") is True
    def test_public(self):
        assert _is_private_ip("93.184.216.34") is False


# ===========================================================================
# UNIT TESTS — Filename Safety
# ===========================================================================

class TestFilename:

    def test_basic(self):
        name = _safe_filename("https://example.com/photo.jpg")
        assert name == "photo.jpg"

    def test_strips_query(self):
        name = _safe_filename("https://example.com/pic.png?w=800")
        assert "?" not in name
        assert name.endswith(".png")

    def test_unsafe_chars_removed(self):
        name = _safe_filename('https://example.com/<script>.jpg')
        assert "<" not in name and ">" not in name

    def test_no_extension_gets_jpg(self):
        name = _safe_filename("https://example.com/image")
        assert name.endswith(".jpg")

    def test_collision_safe(self, tmp_path):
        (tmp_path / "photo.jpg").touch()
        from app import _collision_safe_path
        path = _collision_safe_path(tmp_path, "photo.jpg")
        assert path.name == "photo_1.jpg"


# ===========================================================================
# INTEGRATION TESTS — API (mocked browser fetch)
# ===========================================================================

class TestScanAPI:

    def test_missing_url(self, client):
        resp = client.post("/api/scan", json={})
        assert resp.status_code == 400

    def test_blank_url(self, client):
        resp = client.post("/api/scan", json={"url": ""})
        assert resp.status_code == 400

    def test_blocked_url(self, client):
        resp = client.post("/api/scan", json={"url": "http://127.0.0.1"})
        assert resp.status_code == 403

    def test_unreachable_host(self, client):
        resp = client.post("/api/scan", json={"url": "https://this-domain-does-not-exist-xyz123.com"})
        assert resp.status_code in (403, 502)

    @patch("app.get_browser_worker")
    @patch("app.socket.getaddrinfo")
    def test_successful_scan(self, mock_gai, mock_worker_fn, client):
        mock_gai.return_value = [(2, 1, 0, '', ('93.184.216.34', 0))]
        mock_worker = MagicMock()
        mock_worker.fetch.return_value = (
            '<html><body><img src="/test.jpg" alt="Test"></body></html>',
            "https://example.com",
            []
        )
        mock_worker_fn.return_value = mock_worker
        resp = client.post("/api/scan", json={"url": "https://example.com"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 1
        assert data["images"][0]["url"] == "https://example.com/test.jpg"


class TestDownloadAPI:

    def test_empty_list(self, client):
        resp = client.post("/api/download", json={"urls": [], "source": ""})
        assert resp.status_code == 400

    def test_too_many(self, client):
        urls = [f"https://example.com/{i}.jpg" for i in range(200)]
        resp = client.post("/api/download", json={"urls": urls, "source": ""})
        assert resp.status_code == 400
        assert "Too many" in resp.get_json()["error"]

    @patch("app.http_requests.get")
    def test_successful_download(self, mock_get, client):
        pixel = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.iter_content = MagicMock(return_value=[pixel])
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        resp = client.post("/api/download", json={
            "urls": ["https://example.com/pixel.png"],
            "source": "https://example.com"
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["saved"] == 1
        assert IMAGES_DIR.exists()

    @patch("app.http_requests.get")
    def test_collision_safe_naming(self, mock_get, client):
        pixel = b'\x89PNG\r\n\x1a\n' + b'\x00' * 50
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.iter_content = MagicMock(return_value=[pixel])
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        client.post("/api/download", json={"urls": ["https://example.com/dup.png"], "source": ""})
        resp = client.post("/api/download", json={"urls": ["https://example.com/dup.png"], "source": ""})
        data = resp.get_json()
        assert data["results"][0]["ok"] is True
        files = list(IMAGES_DIR.glob("dup*.png"))
        assert len(files) == 2


# ===========================================================================
# SECURITY TESTS
# ===========================================================================

class TestSecurity:

    def test_ssrf_localhost(self, client):
        resp = client.post("/api/scan", json={"url": "http://localhost/admin"})
        assert resp.status_code == 403

    def test_ssrf_internal_ip(self, client):
        resp = client.post("/api/scan", json={"url": "http://10.0.0.1"})
        assert resp.status_code == 403

    def test_ssrf_metadata_endpoint(self, client):
        resp = client.post("/api/scan", json={"url": "http://169.254.169.254/latest/meta-data"})
        assert resp.status_code == 403

    @patch("app.http_requests.get")
    def test_non_image_rejected(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.iter_content = MagicMock(return_value=[b"<html>not an image</html>"])
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        resp = client.post("/api/download", json={
            "urls": ["https://example.com/notimage.html"],
            "source": ""
        })
        data = resp.get_json()
        assert data["results"][0]["ok"] is False

    def test_path_traversal_image_route(self, client):
        resp = client.get("/images/../../etc/passwd")
        assert resp.status_code in (403, 404)

    def test_security_headers(self, client):
        resp = client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"


# ===========================================================================
# EDGE CASE TESTS
# ===========================================================================

class TestEdgeCases:

    def test_page_with_no_images(self):
        html = "<html><body><h1>Hello</h1></body></html>"
        result = extract_images(html, "https://example.com")
        assert result == []

    @patch("app.http_requests.get")
    def test_mixed_good_bad_urls(self, mock_get, client):
        def side_effect(url, **kwargs):
            if "bad" in url:
                raise Exception("Connection refused")
            mock_r = MagicMock()
            mock_r.status_code = 200
            mock_r.headers = {"Content-Type": "image/png"}
            mock_r.iter_content = MagicMock(return_value=[b'\x89PNG' + b'\x00' * 20])
            mock_r.raise_for_status = MagicMock()
            return mock_r
        mock_get.side_effect = side_effect

        resp = client.post("/api/download", json={
            "urls": ["https://example.com/good.png", "https://example.com/bad.png"],
            "source": ""
        })
        data = resp.get_json()
        assert data["saved"] == 1
        assert data["total"] == 2

    def test_duplicate_in_extraction(self):
        html = '<img src="/a.jpg"><img src="/a.jpg"><img src="/b.jpg">'
        result = extract_images(html, "https://example.com")
        assert len(result) == 2
