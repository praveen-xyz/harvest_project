"""
=============================================================================
  Web Image Extractor — Flask Backend
  Project : Witree Web-Based Image Grabber
  Author  : Witree Team
  Python  : 3.9+
  Server  : Flask (dev) / Waitress (production)
=============================================================================

Sections
--------
  1. Imports & Configuration
  2. Security / Validation Helpers
  3. Image Extraction Helpers
  4. File / Download Helpers
  5. Flask App & CORS Setup
  6. API Routes  (/health, /extract, /download, /proxy, /hosted-zip)
  7. Entry Point (dev vs production)
"""

# =============================================================================
# 1. IMPORTS & CONFIGURATION
# =============================================================================

import os
import re
import io
import time
import socket
import hashlib
import zipfile
import ipaddress
import mimetypes
import unicodedata
import urllib.parse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context, send_file

# ---------------------------------------------------------------------------
# Logging setup (beginner-friendly: logs to console)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable constants — change these to suit your needs
# ---------------------------------------------------------------------------
MAX_IMAGES        = 150       # Maximum images returned per extraction
MAX_FILE_SIZE_MB  = 20        # Skip images larger than this (MB) during download
REQUEST_TIMEOUT   = 12        # Seconds before HTTP request times out
MAX_WORKERS       = 6         # Threads for concurrent image downloads
IMAGES_DIR        = Path(__file__).parent / "images"   # Save folder
USER_AGENT        = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Private IP ranges to block (SSRF protection)
# ---------------------------------------------------------------------------
PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 private
]


# =============================================================================
# 2. SECURITY / VALIDATION HELPERS
# =============================================================================

def is_private_ip(host: str) -> bool:
    """
    Return True if the hostname resolves to a private / loopback / link-local
    IP address.  Blocks SSRF attacks against internal services.
    """
    try:
        # Resolve the hostname to an IP address
        ip_str = socket.gethostbyname(host)
        ip_obj = ipaddress.ip_address(ip_str)
        for network in PRIVATE_RANGES:
            if ip_obj in network:
                return True
    except (socket.gaierror, ValueError):
        # If resolution fails we treat it as potentially private (safe default)
        return True
    return False


def validate_url(url: str) -> tuple[bool, str]:
    """
    Validate a user-supplied URL.

    Returns
    -------
    (True, "")         — URL is safe to fetch
    (False, reason)    — URL failed validation, reason explains why
    """
    if not url:
        return False, "URL is required."

    # Must start with http or https
    if not url.lower().startswith(("http://", "https://")):
        return False, "Only http:// and https:// URLs are allowed."

    # Reject file:// regardless of case
    if url.lower().startswith("file://"):
        return False, "file:// URLs are not allowed."

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Malformed URL."

    if parsed.scheme not in ("http", "https"):
        return False, "Only http and https schemes are allowed."

    host = parsed.hostname or ""
    if not host:
        return False, "URL has no valid hostname."

    # Block localhost by name
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, "Requests to localhost are not allowed."

    # Block private / internal IP ranges
    if is_private_ip(host):
        return False, f"Requests to private or internal IPs are not allowed ({host})."

    return True, ""


# =============================================================================
# 3. IMAGE EXTRACTION HELPERS
# =============================================================================

def fetch_page(url: str) -> tuple[Optional[str], str]:
    """
    Fetch the HTML content of a webpage.

    Returns
    -------
    (html_text, final_url)  — on success
    (None, error_message)   — on failure
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response.text, response.url   # response.url = final URL after redirects
    except requests.exceptions.Timeout:
        return None, f"Request timed out after {REQUEST_TIMEOUT}s."
    except requests.exceptions.TooManyRedirects:
        return None, "Too many redirects."
    except requests.exceptions.SSLError as e:
        return None, f"SSL error: {e}"
    except requests.exceptions.ConnectionError as e:
        return None, f"Connection error: {e}"
    except requests.exceptions.HTTPError as e:
        return None, f"HTTP {response.status_code}: {response.reason}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


def normalise_url(raw_url: str, base_url: str) -> Optional[str]:
    """
    Convert a potentially relative URL to an absolute URL.

    Examples
    --------
    /img/logo.png  + https://example.com  →  https://example.com/img/logo.png
    //cdn.x.com/a.jpg                     →  https://cdn.x.com/a.jpg
    data:image/…                          →  None  (skipped)
    """
    if not raw_url:
        return None

    raw_url = raw_url.strip()

    # Skip data URIs (too large to store meaningfully as URLs)
    if raw_url.lower().startswith("data:"):
        return None

    # Handle protocol-relative URLs
    if raw_url.startswith("//"):
        scheme = urllib.parse.urlparse(base_url).scheme or "https"
        return scheme + ":" + raw_url

    # Already absolute
    if raw_url.lower().startswith(("http://", "https://")):
        return raw_url

    # Relative URL — resolve against base
    try:
        return urllib.parse.urljoin(base_url, raw_url)
    except Exception:
        return None


def extract_srcset_urls(srcset_str: str, base_url: str) -> list[str]:
    """
    Parse a srcset attribute and return all individual image URLs.

    srcset format:  "img-320.jpg 320w, img-640.jpg 640w, img-1280.jpg 2x"
    """
    urls = []
    if not srcset_str:
        return urls
    for part in srcset_str.split(","):
        # Each part is:  "<url> <optional-descriptor>"
        tokens = part.strip().split()
        if tokens:
            url = normalise_url(tokens[0], base_url)
            if url:
                urls.append(url)
    return urls


def extract_images(html: str, base_url: str) -> list[str]:
    """
    Parse HTML and collect image URLs from multiple sources:

      • <img src="…">
      • <img srcset="…">
      • <img data-src="…">       (lazy-load)
      • <img data-lazy-src="…">  (lazy-load variant)
      • <meta property="og:image" content="…">   (OpenGraph)
      • <meta name="twitter:image" content="…">  (Twitter card)
      • <source srcset="…">       (inside <picture>)
      • <link rel="image_src">    (canonical image)

    Deduplicates results and enforces MAX_IMAGES limit.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[str] = []

    def add(url: Optional[str]) -> None:
        """Add URL to results if it is valid and not already seen."""
        if not url:
            return
        # Normalise query-string / fragment for dedup
        url = url.strip()
        if url in seen:
            return
        seen.add(url)
        if len(results) < MAX_IMAGES:
            results.append(url)

    # ------------------------------------------------------------------
    # <img> tags — src, srcset, data-src, data-lazy-src
    # ------------------------------------------------------------------
    for img in soup.find_all("img"):
        add(normalise_url(img.get("src", ""), base_url))
        add(normalise_url(img.get("data-src", ""), base_url))
        add(normalise_url(img.get("data-lazy-src", ""), base_url))
        add(normalise_url(img.get("data-original", ""), base_url))
        add(normalise_url(img.get("data-lazy", ""), base_url))

        for u in extract_srcset_urls(img.get("srcset", ""), base_url):
            add(u)

    # ------------------------------------------------------------------
    # <source> inside <picture> — srcset
    # ------------------------------------------------------------------
    for source in soup.find_all("source"):
        for u in extract_srcset_urls(source.get("srcset", ""), base_url):
            add(u)

    # ------------------------------------------------------------------
    # OpenGraph   <meta property="og:image" content="…">
    # ------------------------------------------------------------------
    for meta in soup.find_all("meta", property="og:image"):
        add(normalise_url(meta.get("content", ""), base_url))

    # ------------------------------------------------------------------
    # Twitter card  <meta name="twitter:image" content="…">
    # ------------------------------------------------------------------
    for meta in soup.find_all("meta", {"name": re.compile(r"^twitter:image", re.I)}):
        add(normalise_url(meta.get("content", ""), base_url))

    # ------------------------------------------------------------------
    # <link rel="image_src">
    # ------------------------------------------------------------------
    for link in soup.find_all("link", rel=lambda r: r and "image_src" in r):
        add(normalise_url(link.get("href", ""), base_url))

    log.info("Extracted %d unique image URLs (limit %d)", len(results), MAX_IMAGES)
    return results


# =============================================================================
# 4. FILE / DOWNLOAD HELPERS
# =============================================================================

def safe_filename(url: str, existing: set[str]) -> str:
    """
    Generate a safe, collision-free filename from a URL.

    Steps
    -----
    1. Extract the URL path's basename.
    2. Strip / replace unsafe characters.
    3. If empty or collides, append a short hash of the URL.
    4. Ensure the final name does not already exist in *existing*.
    """
    parsed    = urllib.parse.urlparse(url)
    path_part = urllib.parse.unquote(parsed.path)
    basename  = os.path.basename(path_part) or "image"

    # Remove leading dots and dangerous characters
    # Allow letters, digits, underscore, hyphen, dot
    basename = unicodedata.normalize("NFKD", basename)
    basename = re.sub(r"[^\w\-.]", "_", basename)
    basename = basename.lstrip(".")
    basename = basename[:120]   # Reasonable length cap

    if not basename or basename == "_":
        basename = "image"

    # Append extension if missing
    if "." not in basename:
        # Try to guess from URL
        ext = mimetypes.guess_extension(
            mimetypes.guess_type(url)[0] or ""
        ) or ".jpg"
        basename += ext

    # Collision prevention: append short hash
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    name, _, ext = basename.rpartition(".")
    candidate = basename

    # If the name already exists in the target set, add hash suffix
    if candidate in existing:
        candidate = f"{name}_{url_hash}.{ext}"

    # Final fallback if still colliding
    if candidate in existing:
        candidate = f"{url_hash}.{ext}"

    return candidate


def download_single_image(
    url: str,
    save_dir: Path,
    existing_names: set[str],
    max_mb: int = MAX_FILE_SIZE_MB,
) -> dict:
    """
    Download one image and save it to *save_dir*.

    Returns a result dict:
    {
        "url"     : original URL,
        "status"  : "ok" | "skipped" | "error",
        "filename": saved filename or "",
        "reason"  : description (on error/skip)
    }
    """
    result = {"url": url, "status": "error", "filename": "", "reason": ""}

    try:
        headers = {"User-Agent": USER_AGENT, "Referer": url}

        # Stream so we can check Content-Length before fully downloading
        with requests.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT,
            stream=True, allow_redirects=True
        ) as resp:
            resp.raise_for_status()

            # Check Content-Length if provided
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_mb * 1024 * 1024:
                result["status"] = "skipped"
                result["reason"] = (
                    f"File too large ({int(content_length) // 1024} KB > "
                    f"{max_mb} MB limit)"
                )
                return result

            # Determine filename
            filename = safe_filename(url, existing_names)
            save_path = save_dir / filename

            # Download in chunks to limit memory usage
            total_bytes = 0
            chunks = []
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    total_bytes += len(chunk)
                    if total_bytes > max_mb * 1024 * 1024:
                        result["status"] = "skipped"
                        result["reason"] = f"File exceeded {max_mb} MB during download"
                        return result
                    chunks.append(chunk)

            # Write to disk atomically (write to tmp, then rename)
            tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
            tmp_path.write_bytes(b"".join(chunks))
            tmp_path.rename(save_path)

            existing_names.add(filename)
            result["status"]   = "ok"
            result["filename"] = filename
            result["reason"]   = f"{total_bytes // 1024} KB saved"
            log.info("Downloaded: %s → %s", url, filename)

    except requests.exceptions.Timeout:
        result["reason"] = "Timed out"
    except requests.exceptions.HTTPError as e:
        result["reason"] = f"HTTP error: {e}"
    except requests.exceptions.ConnectionError as e:
        result["reason"] = f"Connection error: {e}"
    except OSError as e:
        result["reason"] = f"File system error: {e}"
    except Exception as e:
        result["reason"] = f"Unexpected error: {e}"
        log.exception("Unexpected error downloading %s", url)

    return result


def download_images_concurrent(urls: list[str], save_dir: Path) -> list[dict]:
    """
    Download multiple images concurrently using a thread pool.

    Uses a shared set of existing filenames to prevent collisions across
    threads.  Lock-free because Python's GIL protects simple set operations,
    but we keep thread count modest (MAX_WORKERS) to be safe.
    """
    # Seed existing names with files already in the directory
    existing: set[str] = {f.name for f in save_dir.iterdir() if f.is_file()}

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {
            pool.submit(download_single_image, url, save_dir, existing): url
            for url in urls
        }
        for future in as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as exc:
                url = future_map[future]
                results.append({
                    "url": url, "status": "error",
                    "filename": "", "reason": str(exc)
                })
    return results


# =============================================================================
# 5. FLASK APP & CORS SETUP
# =============================================================================

app = Flask(__name__, static_folder=".", static_url_path="")


@app.after_request
def add_cors_headers(response):
    """
    Add CORS headers to every response so the frontend (served from the same
    origin or a dev server) can communicate with the Flask API.
    """
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.route("/", methods=["GET"])
def serve_index():
    """Serve index.html for the root path."""
    return send_from_directory(".", "index.html")


@app.route("/<path:filename>", methods=["GET"])
def serve_static(filename):
    """Serve any other static file (CSS, JS, images, logo, etc.)."""
    return send_from_directory(".", filename)


# =============================================================================
# 6a. PROXY DOWNLOAD  — streams an image to the browser as an attachment
# =============================================================================

@app.route("/proxy", methods=["GET", "OPTIONS"])
def proxy_download():
    """
    Proxy an image from a remote URL and deliver it to the browser as a
    file download (Content-Disposition: attachment).

    This lets the user save any extracted image directly to their local
    Downloads folder — no server-side storage required.

    Usage
    -----
    GET /proxy?url=https%3A%2F%2Fexample.com%2Fphoto.jpg

    The browser receives the image bytes with the correct MIME type and
    a filename derived from the URL, triggering the native Save-As dialog.

    Security
    --------
    The URL is validated with the same rules as /extract and /download:
    only public http/https URLs are accepted; localhost and private IP
    ranges are blocked.
    """
    if request.method == "OPTIONS":
        return "", 204

    raw_url = request.args.get("url", "").strip()

    # ---- Validate URL ----
    ok, reason = validate_url(raw_url)
    if not ok:
        return jsonify({"success": False, "error": reason}), 400

    log.info("Proxy download: %s", raw_url)

    # ---- Build a safe filename for the Content-Disposition header ----
    filename = safe_filename(raw_url, set())

    # ---- Fetch the remote image and stream it back ----
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer"   : raw_url,
        }
        remote = requests.get(
            raw_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        remote.raise_for_status()

        # Determine MIME type from the remote Content-Type header,
        # falling back to a safe default.
        content_type = remote.headers.get("Content-Type", "application/octet-stream")
        # Strip any charset suffix (e.g. "image/jpeg; charset=...")
        content_type = content_type.split(";")[0].strip()

        def generate():
            """Yield the remote response body in 64 KB chunks."""
            for chunk in remote.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        # RFC 5987 — quote the filename for use in the HTTP header
        safe_name = filename.replace('"', "'")

        response = Response(
            stream_with_context(generate()),
            content_type=content_type,
        )
        # 'attachment' forces the browser to download rather than display
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{safe_name}"'
        )
        # Pass through Content-Length if the server provided it
        if "Content-Length" in remote.headers:
            response.headers["Content-Length"] = remote.headers["Content-Length"]

        return response

    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Request timed out."}), 504
    except requests.exceptions.HTTPError as exc:
        return jsonify({"success": False, "error": f"Remote server error: {exc}"}), 502
    except requests.exceptions.ConnectionError as exc:
        return jsonify({"success": False, "error": f"Connection error: {exc}"}), 502
    except Exception as exc:
        log.exception("Proxy error for %s", raw_url)
        return jsonify({"success": False, "error": f"Unexpected error: {exc}"}), 500


# =============================================================================
# 6b. HOSTED ZIP DOWNLOAD — bundles images into a ZIP and sends to browser
# =============================================================================

@app.route("/hosted-zip", methods=["POST", "OPTIONS"])
def hosted_zip():
    """
    Fetch selected images concurrently, bundle them into an in-memory ZIP
    archive, and stream the archive to the browser as a file download.

    This is the Hosted Download Mode endpoint.  No files are permanently
    stored on the server — the ZIP exists only in RAM and is garbage-
    collected once the HTTP response is fully sent.

    Request body (JSON)
    -------------------
    { "images": ["https://...", "https://...", ...] }

    Response 200
    ------------
    Binary ZIP file  (Content-Disposition: attachment; filename="witree-images.zip")

    Response 4xx / 5xx
    ------------------
    { "success": false, "error": "…" }
    """
    if request.method == "OPTIONS":
        return "", 204

    # ---- Parse request body ----
    data = request.get_json(silent=True)
    if not data or "images" not in data:
        return jsonify({"success": False, "error": "Missing 'images' list."}), 400

    urls = data["images"]
    if not isinstance(urls, list) or len(urls) == 0:
        return jsonify({"success": False, "error": "No images provided."}), 400

    if len(urls) > MAX_IMAGES:
        return jsonify({
            "success": False,
            "error"  : f"Too many images (max {MAX_IMAGES})."
        }), 400

    # ---- Validate each URL ----
    valid_urls = []
    for url in urls:
        if isinstance(url, str):
            ok, _ = validate_url(url.strip())
            if ok:
                valid_urls.append(url.strip())

    if not valid_urls:
        return jsonify({"success": False, "error": "No valid image URLs provided."}), 400

    log.info("Hosted ZIP requested: %d image(s)", len(valid_urls))

    # ---- Download all images into memory concurrently ----
    def fetch_to_memory(url: str) -> tuple:
        """
        Download one image into a bytes buffer.
        Returns (url, bytes_or_None, status_message).
        """
        try:
            headers = {"User-Agent": USER_AGENT, "Referer": url}
            with requests.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT,
                stream=True, allow_redirects=True
            ) as resp:
                resp.raise_for_status()
                chunks = []
                total  = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        total += len(chunk)
                        if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                            return url, None, f"exceeded {MAX_FILE_SIZE_MB} MB"
                        chunks.append(chunk)
                return url, b"".join(chunks), "ok"
        except requests.exceptions.Timeout:
            return url, None, "timed out"
        except requests.exceptions.HTTPError as exc:
            return url, None, f"HTTP error: {exc}"
        except Exception as exc:
            return url, None, str(exc)

    # Fetch concurrently, collect into list (order preserved via futures list)
    memory_images = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_to_memory, u) for u in valid_urls]
        for future in as_completed(futures):
            memory_images.append(future.result())

    # ---- Assemble ZIP in memory ----
    zip_buffer = io.BytesIO()
    used_names : set[str] = set()
    added = 0

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for url, img_bytes, status in memory_images:
            if img_bytes is None:
                log.warning("Skipped (hosted-zip): %s — %s", url, status)
                continue
            fname = safe_filename(url, used_names)
            used_names.add(fname)
            zf.writestr(fname, img_bytes)
            added += 1

    if added == 0:
        return jsonify({"success": False, "error": "No images could be downloaded."}), 502

    zip_buffer.seek(0)
    log.info("Hosted ZIP ready: %d image(s), %.1f KB", added, len(zip_buffer.getvalue()) / 1024)

    return send_file(
        zip_buffer,
        mimetype        = "application/zip",
        as_attachment   = True,
        download_name   = "witree-images.zip",
    )


# =============================================================================
# 6. API ROUTES
# =============================================================================

# ----------------------------------------------------------------------------
# GET /health
# ----------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health_check():
    """
    Health-check endpoint.

    Returns basic server status so monitoring tools or the frontend can
    confirm the backend is alive.

    Response 200
    ------------
    {
        "status"    : "ok",
        "timestamp" : 1718...,
        "images_dir": "/absolute/path/to/images"
    }
    """
    return jsonify({
        "status"    : "ok",
        "timestamp" : int(time.time()),
        "images_dir": str(IMAGES_DIR),
    }), 200


# ----------------------------------------------------------------------------
# POST /extract
# ----------------------------------------------------------------------------
@app.route("/extract", methods=["POST", "OPTIONS"])
def extract():
    """
    Extract all image URLs from a given webpage.

    Request body (JSON)
    -------------------
    { "url": "https://example.com" }

    Response 200
    ------------
    {
        "success"  : true,
        "url"      : "https://example.com",
        "count"    : 42,
        "images"   : ["https://...", ...],
        "elapsed_s": 1.23
    }

    Response 4xx / 5xx
    ------------------
    { "success": false, "error": "Human-readable reason" }
    """
    # Handle pre-flight OPTIONS request (CORS)
    if request.method == "OPTIONS":
        return "", 204

    t_start = time.time()

    # ---- Parse request body ----
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"success": False, "error": "Missing 'url' in request body."}), 400

    raw_url: str = str(data["url"]).strip()

    # ---- Validate URL ----
    ok, reason = validate_url(raw_url)
    if not ok:
        return jsonify({"success": False, "error": reason}), 400

    log.info("Extraction requested: %s", raw_url)

    # ---- Fetch the page ----
    html, final_url = fetch_page(raw_url)
    if html is None:
        # final_url holds the error message when html is None
        return jsonify({"success": False, "error": final_url}), 502

    # ---- Extract images ----
    images = extract_images(html, final_url)
    elapsed = round(time.time() - t_start, 2)

    log.info("Extraction complete: %d images in %.2fs", len(images), elapsed)

    return jsonify({
        "success"  : True,
        "url"      : final_url,
        "count"    : len(images),
        "images"   : images,
        "elapsed_s": elapsed,
    }), 200


# ----------------------------------------------------------------------------
# POST /download
# ----------------------------------------------------------------------------
@app.route("/download", methods=["POST", "OPTIONS"])
def download():
    """
    Download selected images to the server's images/ folder.

    Request body (JSON)
    -------------------
    { "images": ["https://...", "https://...", ...] }

    Response 200
    ------------
    {
        "success"  : true,
        "requested": 5,
        "saved"    : 4,
        "skipped"  : 1,
        "errors"   : 0,
        "results"  : [
            { "url": "…", "status": "ok",      "filename": "img.jpg", "reason": "42 KB saved" },
            { "url": "…", "status": "skipped",  "filename": "",        "reason": "Too large"  },
            ...
        ]
    }

    Response 4xx / 5xx
    ------------------
    { "success": false, "error": "…" }
    """
    if request.method == "OPTIONS":
        return "", 204

    # ---- Parse request body ----
    data = request.get_json(silent=True)
    if not data or "images" not in data:
        return jsonify({"success": False, "error": "Missing 'images' list in request body."}), 400

    urls = data["images"]
    if not isinstance(urls, list):
        return jsonify({"success": False, "error": "'images' must be a JSON array."}), 400

    if len(urls) == 0:
        return jsonify({"success": False, "error": "No images selected."}), 400

    if len(urls) > MAX_IMAGES:
        return jsonify({
            "success": False,
            "error"  : f"Too many images requested. Maximum is {MAX_IMAGES}."
        }), 400

    # ---- Validate each URL ----
    valid_urls = []
    for url in urls:
        if not isinstance(url, str):
            continue
        ok, _ = validate_url(url.strip())
        if ok:
            valid_urls.append(url.strip())

    if not valid_urls:
        return jsonify({"success": False, "error": "No valid image URLs provided."}), 400

    # ---- Ensure images/ directory exists ----
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Download requested: %d image(s)", len(valid_urls))

    # ---- Download concurrently ----
    results = download_images_concurrent(valid_urls, IMAGES_DIR)

    # ---- Summarise ----
    saved   = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors  = sum(1 for r in results if r["status"] == "error")

    log.info("Download complete: %d saved, %d skipped, %d errors", saved, skipped, errors)

    return jsonify({
        "success"  : True,
        "requested": len(valid_urls),
        "saved"    : saved,
        "skipped"  : skipped,
        "errors"   : errors,
        "results"  : results,
    }), 200


# =============================================================================
# 7. ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # Ensure images directory exists on startup
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Images will be saved to: %s", IMAGES_DIR)

    import sys

    # Use Waitress in production when "production" flag is passed
    # Usage:  python app.py production
    use_production = len(sys.argv) > 1 and sys.argv[1].lower() == "production"

    if use_production:
        # ---- Waitress production server ----
        try:
            from waitress import serve
            log.info("Starting Waitress production server on http://0.0.0.0:5000")
            serve(app, host="0.0.0.0", port=5000, threads=MAX_WORKERS * 2)
        except ImportError:
            log.error(
                "Waitress is not installed. Run: pip install waitress\n"
                "Falling back to Flask dev server."
            )
            app.run(debug=False, host="0.0.0.0", port=5000)
    else:
        # ---- Flask development server ----
        log.info("Starting Flask development server on http://127.0.0.1:5000")
        log.info("Tip: run  python app.py production  to use Waitress instead.")
        app.run(debug=True, host="127.0.0.1", port=5000)
