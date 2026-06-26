"""
Project Harvest — Web-Based Image Grabber Application
Backend server built with Flask + Playwright.

WiTree Technology Solutions Pvt Ltd
Engineering Training & Development
"""

import os
import re
import io
import zipfile
import socket
import ipaddress
import hashlib
import logging
import threading
import concurrent.futures
from queue import Queue
from urllib.parse import urljoin, urlparse, unquote
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, send_file, abort
from bs4 import BeautifulSoup
import requests as http_requests

# ---------------------------------------------------------------------------
# Configuration — all tuneable via environment variables
# ---------------------------------------------------------------------------
HOST = os.environ.get("HARVEST_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("HARVEST_PORT", "5000")))
DEV_MODE = os.environ.get("HARVEST_DEV", "false").lower() == "true"

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / os.environ.get("HARVEST_IMAGE_FOLDER", "images")

MAX_PAGE_SIZE = int(os.environ.get("HARVEST_MAX_PAGE_SIZE", str(10 * 1024 * 1024)))
MAX_IMAGE_SIZE = int(os.environ.get("HARVEST_MAX_IMAGE_SIZE", str(25 * 1024 * 1024)))
MAX_GRAB_COUNT = int(os.environ.get("HARVEST_MAX_GRAB_COUNT", "100"))
MAX_REQUEST_BODY = int(os.environ.get("HARVEST_MAX_REQUEST_BODY", str(1 * 1024 * 1024)))
REQUEST_TIMEOUT = int(os.environ.get("HARVEST_TIMEOUT", "25"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

ALLOWED_IMAGE_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/svg+xml", "image/bmp", "image/tiff", "image/x-icon",
    "image/vnd.microsoft.icon", "image/avif",
}

ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".bmp", ".tiff", ".tif", ".ico", ".avif",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger("harvest")

# Check if Playwright is actually installed before we ever try to use it
try:
    import importlib
    importlib.import_module("playwright")
    PLAYWRIGHT_INSTALLED = True
    logger.info("Playwright module found.")
except ImportError:
    PLAYWRIGHT_INSTALLED = False
    logger.info("Playwright not installed — using fast HTTP mode only.")

# Reusable session with connection pooling for faster HTTP requests
_http_session = http_requests.Session()
_http_session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY


# ---------------------------------------------------------------------------
# Playwright browser worker — runs in its OWN dedicated thread
# ---------------------------------------------------------------------------

class BrowserWorker:
    """
    Runs Playwright in a single dedicated thread to avoid greenlet conflicts.
    All browser work is submitted via .fetch(url) which blocks the caller
    until the result is ready.
    """

    def __init__(self):
        self._queue = Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(timeout=30)
        logger.info("Browser worker ready.")

    def _run(self):
        """Entry point for the browser thread."""
        if not PLAYWRIGHT_INSTALLED:
            self._ready.set()
            return

        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        logger.info("Playwright Chromium launched in worker thread.")
        self._ready.set()

        while True:
            url, result_holder, event = self._queue.get()
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    java_script_enabled=True,
                    ignore_https_errors=True,
                )
                page = context.new_page()

                # Block heavy resources we don't need — speeds up scan
                page.route("**/*.{mp4,avi,mov,mkv,flv,wmv,webm}", lambda route: route.abort())
                page.route("**/*.{woff,woff2,ttf,otf,eot}", lambda route: route.abort())

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
                    page.wait_for_timeout(800)

                    # Scroll the full page to trigger ALL lazy-loaded images
                    page.evaluate("""
                        () => new Promise(resolve => {
                            let y = 0;
                            const step = 600;
                            const limit = document.body.scrollHeight;
                            const id = setInterval(() => {
                                window.scrollBy(0, step);
                                y += step;
                                if (y >= limit) { clearInterval(id); window.scrollTo(0,0); resolve(); }
                            }, 30);
                        })
                    """)
                    page.wait_for_timeout(800)

                    # Extract images via JS DOM API — catches everything the browser rendered
                    js_images = page.evaluate(r"""
                        () => {
                            const imgs = new Set();
                            const addUrl = (u) => {
                                if (!u || u.startsWith('data:') || u.startsWith('blob:')) return;
                                try { imgs.add(new URL(u, location.href).href); } catch {}
                            };

                            // 1. performance API — catches ALL loaded image resources
                            try {
                                const imgExts = /\.(jpg|jpeg|png|gif|webp|svg|avif|bmp|ico|tiff)/i;
                                performance.getEntriesByType('resource').forEach(r => {
                                    if (r.initiatorType === 'img' || r.initiatorType === 'css' ||
                                        r.initiatorType === 'link' || r.initiatorType === 'other' ||
                                        r.initiatorType === 'fetch' || r.initiatorType === 'xmlhttprequest' ||
                                        imgExts.test(r.name) ||
                                        /image/i.test(r.name)) {
                                        addUrl(r.name);
                                    }
                                });
                            } catch {}

                            // 2. All <img> elements — src, currentSrc, srcset, ALL data-* attrs
                            document.querySelectorAll('img').forEach(el => {
                                addUrl(el.currentSrc);
                                addUrl(el.src);
                                // Check ALL attributes for image URLs
                                for (const attr of el.attributes) {
                                    const v = attr.value;
                                    if (v && v !== el.src && (v.startsWith('http') || v.startsWith('/') || v.startsWith('.')) &&
                                        /\.(jpg|jpeg|png|gif|webp|svg|avif|bmp|ico)/i.test(v)) {
                                        addUrl(v);
                                    }
                                }
                                if (el.srcset) {
                                    el.srcset.split(',').forEach(e => {
                                        const u = e.trim().split(/\s+/)[0];
                                        if (u) addUrl(u);
                                    });
                                }
                            });

                            // 3. <picture><source> srcset
                            document.querySelectorAll('source[srcset]').forEach(el => {
                                el.srcset.split(',').forEach(e => {
                                    const u = e.trim().split(/\s+/)[0];
                                    if (u) addUrl(u);
                                });
                                addUrl(el.src);
                            });

                            // 4. <link> — icons, preloads, apple-touch-icon
                            document.querySelectorAll('link[rel*="icon"], link[rel="apple-touch-icon"], link[as="image"]').forEach(el => {
                                addUrl(el.href);
                            });

                            // 5. <meta> — og:image, twitter:image
                            document.querySelectorAll('meta[property*="image"], meta[name*="image"], meta[property="og:image"], meta[name="twitter:image"]').forEach(el => {
                                addUrl(el.content);
                            });

                            // 6. CSS background-image on ALL visible elements
                            document.querySelectorAll('*').forEach(el => {
                                try {
                                    const bg = getComputedStyle(el).backgroundImage;
                                    if (bg && bg !== 'none') {
                                        const re = /url\(["']?(.+?)["']?\)/g;
                                        let m;
                                        while ((m = re.exec(bg)) !== null) {
                                            addUrl(m[1]);
                                        }
                                    }
                                } catch {}
                            });

                            // 7. <input type="image">
                            document.querySelectorAll('input[type="image"]').forEach(el => {
                                addUrl(el.src);
                            });

                            // 8. <svg> <image> elements
                            document.querySelectorAll('image').forEach(el => {
                                addUrl(el.getAttribute('href'));
                                addUrl(el.getAttribute('xlink:href'));
                            });

                            // 9. <video poster>
                            document.querySelectorAll('video[poster]').forEach(el => {
                                addUrl(el.poster);
                            });

                            // 10. <object> and <embed>
                            document.querySelectorAll('object[data], embed[src]').forEach(el => {
                                const u = el.data || el.src;
                                if (/\.(jpg|jpeg|png|gif|webp|svg|avif|bmp|ico)/i.test(u)) addUrl(u);
                            });

                            // Filter: only http(s), skip tracking pixels and data URIs
                            return [...imgs].filter(u => u.startsWith('http'));
                        }
                    """)

                    html = page.content()
                    final_url = page.url
                    result_holder["html"] = html
                    result_holder["url"] = final_url
                    result_holder["js_images"] = js_images or []
                except Exception as e:
                    result_holder["error"] = str(e)
                finally:
                    context.close()
            except Exception as e:
                result_holder["error"] = str(e)
            event.set()

    def fetch(self, url: str) -> tuple[str, str, list[str]]:
        """
        Submit a URL to the browser worker and block until done.
        Returns (html, final_url, js_image_urls). Raises on error.
        """
        result = {}
        event = threading.Event()
        self._queue.put((url, result, event))
        event.wait(timeout=REQUEST_TIMEOUT + 15)

        if "error" in result:
            raise RuntimeError(result["error"])
        if "html" not in result:
            raise RuntimeError("Browser fetch timed out.")

        html = result["html"]
        if len(html.encode("utf-8", errors="replace")) > MAX_PAGE_SIZE:
            raise ValueError("Page exceeds maximum allowed size.")

        return html, result["url"], result.get("js_images", [])


# Create the singleton worker at module level (started on first import)
_browser_worker = None
_playwright_available = PLAYWRIGHT_INSTALLED


def get_browser_worker():
    global _browser_worker, _playwright_available
    if not _playwright_available:
        return None
    if _browser_worker is None:
        try:
            _browser_worker = BrowserWorker()
        except Exception as e:
            logger.warning("Playwright unavailable: %s", e)
            _playwright_available = False
            return None
    return _browser_worker


# ---------------------------------------------------------------------------
# Fast HTTP fetcher (requests + connection pooling — primary strategy)
# ---------------------------------------------------------------------------

def _fetch_with_requests(url: str) -> tuple[str, str]:
    """
    Fetch a page using pooled HTTP session.  Returns (html, final_url).
    Typically completes in < 2 seconds.
    """
    resp = _http_session.get(
        url,
        timeout=min(REQUEST_TIMEOUT, 10),  # cap at 10s for speed
        allow_redirects=True,
    )
    resp.raise_for_status()

    if len(resp.content) > MAX_PAGE_SIZE:
        raise ValueError("Page exceeds maximum allowed size.")

    return resp.text, resp.url


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _is_private_ip(ip_str: str) -> bool:
    """Return True if the IP is private, loopback, link-local, reserved, etc."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        )
    except ValueError:
        return True


def validate_url(raw_url: str) -> str:
    """Validate and normalise a user-supplied URL."""
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("URL must not be empty.")

    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://", raw_url)
    if scheme_match and scheme_match.group(1).lower() not in ("http", "https"):
        raise ValueError("Only http and https URLs are allowed.")

    if not re.match(r"^https?://", raw_url, re.IGNORECASE):
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Only http and https URLs are allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no valid hostname.")

    if not DEV_MODE:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _type, _proto, _canon, sockaddr in infos:
                if _is_private_ip(sockaddr[0]):
                    raise ValueError(
                        f"Blocked: '{hostname}' resolves to a private/reserved address."
                    )
        except socket.gaierror:
            raise ValueError(f"Cannot resolve hostname '{hostname}'.")

    return raw_url


def _validate_redirect_url(final_url: str) -> None:
    """SR-02: re-validate the final URL after redirects."""
    if DEV_MODE:
        return
    parsed = urlparse(final_url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Redirect led to an invalid URL.")
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _type, _proto, _canon, sockaddr in infos:
            if _is_private_ip(sockaddr[0]):
                raise ValueError(
                    f"Blocked: redirect target '{hostname}' resolves to a private address."
                )
    except socket.gaierror:
        raise ValueError(f"Cannot resolve redirect target '{hostname}'.")


# ---------------------------------------------------------------------------
# Image extraction logic
# ---------------------------------------------------------------------------

def _resolve(base: str, url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("data:"):
        return None
    return urljoin(base, url)


def _parse_srcset(srcset: str) -> list[str]:
    urls = []
    for entry in srcset.split(","):
        parts = entry.strip().split()
        if parts:
            urls.append(parts[0])
    return urls


def extract_images(html: str, page_url: str) -> list[dict]:
    """FR-04: Extract image URLs from rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    images: list[dict] = []

    def _add(url: str | None, alt: str = ""):
        if url is None:
            return
        resolved = _resolve(page_url, url)
        if resolved and resolved not in seen:
            seen.add(resolved)
            images.append({"url": resolved, "alt": alt})

    lazy_attrs = ["data-src", "data-lazy", "data-original", "data-lazy-src",
                  "data-srcset", "data-hi-res-src"]
    for img in soup.find_all("img"):
        alt_text = img.get("alt", "")
        _add(img.get("src"), alt_text)
        for attr in lazy_attrs:
            val = img.get(attr)
            if val:
                if attr == "data-srcset":
                    for u in _parse_srcset(val):
                        _add(u, alt_text)
                else:
                    _add(val, alt_text)
        if img.get("srcset"):
            for u in _parse_srcset(img["srcset"]):
                _add(u, alt_text)

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for u in _parse_srcset(srcset):
                _add(u)
        _add(source.get("src"))

    for link in soup.find_all("link", rel=lambda r: r and "icon" in r):
        _add(link.get("href"))

    bg_regex = re.compile(r"url\(\s*['\"]?(.+?)['\"]?\s*\)", re.IGNORECASE)
    for tag in soup.find_all(style=True):
        for match in bg_regex.finditer(tag["style"]):
            _add(match.group(1))
    for style_tag in soup.find_all("style"):
        if style_tag.string:
            for match in bg_regex.finditer(style_tag.string):
                _add(match.group(1))

    return images


# ---------------------------------------------------------------------------
# File-handling helpers
# ---------------------------------------------------------------------------

def _safe_filename(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    basename = os.path.basename(path) or "image"
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', basename)
    basename = basename.strip('. ')
    if not basename:
        basename = hashlib.md5(url.encode()).hexdigest()[:12]
    name, ext = os.path.splitext(basename)
    if ext.lower() not in ALLOWED_IMAGE_EXTENSIONS:
        ext = ".jpg"
    return (name or "image") + ext


def _collision_safe_path(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem, ext = os.path.splitext(name)
    counter = 1
    while True:
        candidate = folder / f"{stem}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def _is_image_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.split(";")[0].strip().lower()
    return ct in ALLOWED_IMAGE_CONTENT_TYPES


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def _download_one(url: str, referer: str) -> dict:
    try:
        resp = http_requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        resp.raise_for_status()

        ct = resp.headers.get("Content-Type", "")
        original_ext = os.path.splitext(urlparse(url).path)[1].lower()

        if not _is_image_content_type(ct) and original_ext not in ALLOWED_IMAGE_EXTENSIONS:
            return {"url": url, "ok": False, "error": "Not an image (content-type rejected)."}

        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            total += len(chunk)
            if total > MAX_IMAGE_SIZE:
                return {"url": url, "ok": False, "error": "Image exceeds maximum size limit."}
            chunks.append(chunk)

        IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        safe_name = _safe_filename(url)
        target_path = _collision_safe_path(IMAGES_DIR, safe_name)
        if not str(target_path.resolve()).startswith(str(IMAGES_DIR.resolve())):
            return {"url": url, "ok": False, "error": "Filename rejected (path traversal attempt)."}

        target_path.write_bytes(b"".join(chunks))
        return {"url": url, "ok": True, "file": target_path.name}

    except http_requests.RequestException as exc:
        return {"url": url, "ok": False, "error": str(exc)[:200]}
    except Exception as exc:
        return {"url": url, "ok": False, "error": f"Unexpected error: {str(exc)[:200]}"}


# ---------------------------------------------------------------------------
# Security-header middleware
# ---------------------------------------------------------------------------

@app.after_request
def _set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if response.content_type and response.content_type.startswith("image/"):
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.route("/witree_logo.png")
def witree_logo():
    return send_file(BASE_DIR / "witree_logo.png", mimetype="image/png")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(silent=True)
    if not data or not data.get("url"):
        return jsonify({"error": "Missing or empty 'url' field."}), 400

    try:
        url = validate_url(data["url"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 403

    import time
    t0 = time.perf_counter()

    html = None
    final_url = url
    js_images = []

    # --- Strategy 1 (FAST): HTTP requests + BeautifulSoup ---
    try:
        html, final_url = _fetch_with_requests(url)
        _validate_redirect_url(final_url)
        elapsed = time.perf_counter() - t0
        logger.info("Page fetched via requests in %.2fs.", elapsed)
    except ValueError as e:
        return jsonify({"error": str(e)}), 403
    except Exception as e:
        logger.warning("Requests fetch failed: %s", e)
        html = None

    # --- Strategy 2 (FALLBACK): Playwright for JS-heavy sites ---
    if html is None:
        worker = get_browser_worker()
        if worker is not None:
            try:
                html, final_url, js_images = worker.fetch(url)
                _validate_redirect_url(final_url)
                elapsed = time.perf_counter() - t0
                logger.info("Page fetched via Playwright in %.2fs.", elapsed)
            except Exception as e:
                logger.error("Both strategies failed: %s", e)
                return jsonify({"error": f"Failed to fetch page: {str(e)[:300]}"}), 502
        else:
            return jsonify({"error": f"Failed to fetch page: requests error."}), 502

    # Extract from HTML via BeautifulSoup
    images = extract_images(html, final_url)

    # Merge JS DOM-extracted images (catches computed CSS backgrounds, currentSrc, etc.)
    seen = {img["url"] for img in images}
    for js_url in js_images:
        if js_url not in seen:
            seen.add(js_url)
            images.append({"url": js_url, "alt": ""})

    elapsed = time.perf_counter() - t0
    logger.info("Scan complete: %d images in %.2fs.", len(images), elapsed)

    return jsonify({
        "source": final_url,
        "count": len(images),
        "images": images,
    })


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body."}), 400

    urls = data.get("urls", [])
    source = data.get("source", "")

    if not urls:
        return jsonify({"error": "No image URLs provided."}), 400
    if len(urls) > MAX_GRAB_COUNT:
        return jsonify({"error": f"Too many images. Maximum is {MAX_GRAB_COUNT} per grab."}), 400

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_download_one, u, source): u for u in urls}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    saved = sum(1 for r in results if r["ok"])
    return jsonify({
        "saved": saved,
        "total": len(urls),
        "folder": str(IMAGES_DIR),
        "results": results,
    })


@app.route("/api/download-zip", methods=["POST"])
def api_download_zip():
    """Download selected images as a ZIP file to the user's device."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body."}), 400

    urls = data.get("urls", [])
    source = data.get("source", "")

    if not urls:
        return jsonify({"error": "No image URLs provided."}), 400
    if len(urls) > MAX_GRAB_COUNT:
        return jsonify({"error": f"Too many images. Maximum is {MAX_GRAB_COUNT} per grab."}), 400

    # Fetch all images concurrently
    def _fetch_image_bytes(url: str) -> dict:
        try:
            resp = _http_session.get(
                url,
                headers={
                    "Referer": source,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
                timeout=min(REQUEST_TIMEOUT, 10),
                stream=True,
            )
            resp.raise_for_status()
            content = resp.content
            if len(content) > MAX_IMAGE_SIZE:
                return {"url": url, "ok": False, "error": "Too large"}
            return {"url": url, "ok": True, "data": content}
        except Exception as e:
            return {"url": url, "ok": False, "error": str(e)[:100]}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_image_bytes, u): u for u in urls}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    # Build ZIP in memory
    zip_buffer = io.BytesIO()
    seen_names = set()
    saved_count = 0
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            if not r.get("ok"):
                continue
            fname = _safe_filename(r["url"])
            # Avoid duplicate filenames inside the ZIP
            if fname in seen_names:
                name, ext = os.path.splitext(fname)
                counter = 1
                while f"{name}_{counter}{ext}" in seen_names:
                    counter += 1
                fname = f"{name}_{counter}{ext}"
            seen_names.add(fname)
            zf.writestr(fname, r["data"])
            saved_count += 1

    if saved_count == 0:
        return jsonify({"error": "Could not download any images."}), 502

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="harvest_images.zip",
    )


@app.route("/api/proxy-image")
def api_proxy_image():
    """Proxy a single image for direct browser download (bypasses CORS)."""
    img_url = request.args.get("url", "").strip()
    if not img_url:
        return jsonify({"error": "Missing url parameter."}), 400

    try:
        resp = _http_session.get(
            img_url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=min(REQUEST_TIMEOUT, 10),
        )
        resp.raise_for_status()
    except Exception as e:
        return jsonify({"error": f"Failed to fetch image: {str(e)[:200]}"}), 502

    ct = resp.headers.get("Content-Type", "application/octet-stream")
    fname = _safe_filename(img_url)

    return send_file(
        io.BytesIO(resp.content),
        mimetype=ct,
        as_attachment=True,
        download_name=fname,
    )


@app.route("/images/<path:name>")
def serve_image(name):
    safe = Path(IMAGES_DIR / name).resolve()
    if not str(safe).startswith(str(IMAGES_DIR.resolve())):
        abort(403)
    if not safe.is_file():
        abort(404)
    return send_from_directory(IMAGES_DIR, name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Initializing (Playwright will be tried lazily on first scan)...")
    # Don't pre-init Playwright — let it start lazily on first scan request
    # This keeps startup fast and avoids crashes if Chromium isn't available

    if DEV_MODE:
        logger.info("Starting in DEVELOPMENT mode on %s:%s", HOST, PORT)
        app.run(host=HOST, port=PORT, debug=False)
    else:
        from waitress import serve as waitress_serve
        logger.info("Starting production server (Waitress) on %s:%s", HOST, PORT)
        waitress_serve(app, host=HOST, port=PORT)
