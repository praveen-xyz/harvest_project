# 🖼️ Web Image Extractor — Witree

A full-stack Web Image Extractor built with **Python (Flask)** on the backend and **Vanilla HTML/CSS/JavaScript** on the frontend. Paste any public webpage URL, and the tool instantly extracts every image it finds, displays them in a beautiful gallery, lets you select images, and saves them to your local `images/` folder.

---

## 📁 Project Structure

```
witree project/
├── app.py            ← Flask backend (all server logic in one file)
├── index.html        ← Frontend (HTML + CSS + JavaScript, no frameworks)
├── requirements.txt  ← Python dependencies
├── test_app.py       ← Automated test suite (pytest)
├── README.md         ← This file
├── images/           ← Downloaded images are saved here
│   └── .gitkeep
└── witree-logo.jpg   ← Logo used in the UI
```

---

## ⚡ Quick Start

### 1. Prerequisites

- Python 3.9 or newer
- pip

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the server

```bash
# Development mode (auto-reload on code changes)
python app.py

# Production mode (Waitress WSGI server)
python app.py production
```

### 4. Open the app

Open your browser and visit:

```
http://127.0.0.1:5000
```

Paste any public URL (e.g. `https://www.wikipedia.org/wiki/Python`) and click **Grab Images**.

---

## 🔌 REST API Reference

All endpoints return JSON.

---

### `GET /health`

Health check — confirms the server is running.

**Response `200 OK`**
```json
{
  "status": "ok",
  "timestamp": 1718700000,
  "images_dir": "C:/path/to/images"
}
```

---

### `POST /extract`

Fetch a webpage and extract all image URLs from it.

**Request body**
```json
{ "url": "https://example.com" }
```

**Response `200 OK`**
```json
{
  "success": true,
  "url": "https://example.com/",
  "count": 42,
  "images": [
    "https://example.com/hero.jpg",
    "https://cdn.example.com/logo.png",
    "..."
  ],
  "elapsed_s": 1.23
}
```

**Error responses**

| Status | Reason |
|--------|--------|
| `400`  | Missing/invalid/disallowed URL |
| `502`  | Target page could not be fetched |

---

### `POST /download`

Download selected images to the server's `images/` folder.

**Request body**
```json
{
  "images": [
    "https://example.com/photo.jpg",
    "https://cdn.example.com/banner.png"
  ]
}
```

**Response `200 OK`**
```json
{
  "success": true,
  "requested": 2,
  "saved": 2,
  "skipped": 0,
  "errors": 0,
  "results": [
    { "url": "https://example.com/photo.jpg",  "status": "ok",    "filename": "photo.jpg",  "reason": "42 KB saved" },
    { "url": "https://cdn.example.com/banner.png", "status": "ok", "filename": "banner.png", "reason": "88 KB saved" }
  ]
}
```

**Possible `status` values per result:**

| Status | Meaning |
|--------|---------|
| `ok` | Image downloaded successfully |
| `skipped` | File exceeded the size limit |
| `error` | Network or I/O error |

---

## 🔍 What the Extractor Finds

The backend parses multiple image sources from HTML:

| Source | Attribute / Tag |
|--------|----------------|
| Standard images | `<img src="…">` |
| Srcset (responsive) | `<img srcset="…">` |
| Lazy-loaded images | `<img data-src="…">`, `data-lazy-src`, `data-original`, `data-lazy` |
| Responsive picture | `<source srcset="…">` |
| OpenGraph image | `<meta property="og:image">` |
| Twitter card image | `<meta name="twitter:image">` |
| Canonical image link | `<link rel="image_src">` |

Relative URLs are automatically resolved to absolute URLs. Duplicates are removed. Data URIs are ignored.

---

## 🔒 Security Features

- **URL scheme validation** — Only `http://` and `https://` are accepted
- **`file://` URLs blocked** — Prevents local filesystem access
- **`localhost` blocked** — Prevents SSRF to local services
- **Private IP ranges blocked** — `10.x.x.x`, `192.168.x.x`, `172.16.x.x`, `127.x.x.x`, link-local, IPv6 private
- **Request timeouts** — 12 seconds max per HTTP request
- **Image count limit** — Max 150 images per extraction
- **File size limit** — Max 20 MB per downloaded image
- **Safe filename generation** — Sanitises names, prevents path traversal
- **Collision prevention** — Hash suffix added if filename already exists

---

## ⚙️ Configuration

Open `app.py` and change these constants near the top:

```python
MAX_IMAGES        = 150    # Maximum images extracted per page
MAX_FILE_SIZE_MB  = 20     # Max download size per image (MB)
REQUEST_TIMEOUT   = 12     # HTTP timeout (seconds)
MAX_WORKERS       = 6      # Parallel download threads
```

---

## 🧪 Running Tests

```bash
# Install pytest if not already installed
pip install pytest

# Run all tests
python -m pytest test_app.py -v
```

The test suite covers:

| Group | What's tested |
|-------|--------------|
| URL Validation | Valid URLs, blocked schemes, localhost, private IPs |
| Image Extraction | `<img>`, srcset, data-src, og:image, twitter:image, deduplication, limits |
| Download Helpers | Filename sanitisation, collision handling, HTTP errors, size limits |
| API Routes | `/health`, `/extract`, `/download` — success and error cases |

---

## 🚀 Production Deployment

For production use, run with Waitress (already included in `requirements.txt`):

```bash
python app.py production
```

Waitress is a pure-Python WSGI server that is safe and stable for serving small to medium web apps on all platforms (Windows, Linux, macOS).

To serve on a different port:

1. Edit the last lines of `app.py`
2. Change `port=5000` to your desired port

---

## ⚠️ Known Limitations

- **JavaScript-rendered images are not extracted.** The backend fetches raw HTML only — images loaded by React, Vue, or other JavaScript frameworks after page load will not be found.
- **CAPTCHA / bot-protected sites** may reject the request (returns an HTTP error).
- **Very large pages** may time out if the server is slow (timeout is configurable).
- **Data URIs** (Base64 inline images) are intentionally skipped.
- **Images are saved on the server**, not downloaded to your browser. Look inside the `images/` folder next to `app.py`.
- This tool is intended for **publicly accessible pages** only. Respect copyright and terms of service of the websites you scrape.

---

## 🛠️ Technology Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.9+, Flask 3 |
| HTTP client | Requests |
| HTML parser | BeautifulSoup4 + lxml |
| Production server | Waitress |
| Frontend | Vanilla HTML5, CSS3, JavaScript (ES2020) |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` |
| Tests | `unittest` + `pytest` |

---

## 📝 License

This project was created by the **Witree Team** as an academic portfolio project. Feel free to use and modify it for educational purposes.
