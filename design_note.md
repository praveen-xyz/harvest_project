# Design Note — Project Harvest

## Architecture

Project Harvest follows a simple **client-server** architecture:

- **Backend (app.py):** A single Flask application handles URL validation, page fetching, HTML parsing, image downloading, and file management. Waitress serves as the production WSGI server.
- **Frontend (index.html):** A single HTML file with embedded CSS and vanilla JavaScript. No build tools, no frameworks — it loads instantly and works in any modern browser.
- **Storage:** The local filesystem. Downloaded images are saved into an `images/` directory created automatically beside `index.html`.

This keeps the project a **single folder** that works on Windows, Linux, and macOS with zero configuration.

## Key Design Decisions

### 1. Image Extraction Strategy

The extraction logic handles multiple image embedding methods:

- Standard `<img src>` and `srcset` attributes
- Lazy-loading attributes (`data-src`, `data-lazy`, `data-original`)
- `<picture>` / `<source>` elements
- Favicons via `<link rel="icon">`
- CSS `background-image` in both inline styles and `<style>` blocks

All URLs are resolved to absolute using `urllib.parse.urljoin`. Duplicates are tracked via a `set` and excluded from results. `data:` URIs are skipped as they are inline, not downloadable resources.

### 2. Concurrent Downloads

Image downloads use `concurrent.futures.ThreadPoolExecutor` with a pool of 8 workers. This satisfies NFR-02 (perceptible speed improvement over sequential) without overwhelming target servers (SR-08).

### 3. Security Measures

Security is treated as a first-class concern, not an afterthought:

| Threat | Mitigation |
|---|---|
| **SSRF (SR-01/02)** | URL scheme restricted to `http`/`https`. The hostname is resolved via `socket.getaddrinfo` and every resulting IP is checked against `ipaddress` module categories (private, loopback, link-local, reserved, multicast). Redirect targets are re-validated. A `HARVEST_DEV` flag relaxes this for local testing only. |
| **XSS (SR-06)** | All scraped text (alt attributes, filenames) is inserted into the DOM using `textContent`, never `innerHTML`. No user-supplied string is ever interpreted as HTML. |
| **Path Traversal (SR-05)** | Filenames are sanitized by stripping unsafe characters. The final resolved path is checked with `str.startswith(images_dir)` to ensure it stays inside the images folder. The `/images/<name>` route applies the same check. |
| **Content-Type (SR-03)** | Downloads are accepted only when the HTTP `Content-Type` is in an allow-list of image MIME types, or the URL extension is a known image format. |
| **Resource Limits (SR-04)** | Page size, image size, request body size, and grab count are all capped via configurable environment variables. Over-limit downloads are rejected and partial files are never saved. |
| **Security Headers (SR-07)** | Every response includes `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`. Served images get a restrictive `Content-Security-Policy` to neutralize scripts in SVGs. |

### 4. Filename Collision Handling

When two images would produce the same filename (e.g., both called `image.jpg`), the system appends a numeric suffix (`image_1.jpg`, `image_2.jpg`, etc.) rather than overwriting. This is checked atomically via filesystem existence checks before writing.

### 5. Error Isolation

Per NFR-03, each image download is independent. A failure in one (network error, content-type mismatch, size limit) is caught and reported without affecting the others. The UI shows per-image success/failure status after every grab operation.
