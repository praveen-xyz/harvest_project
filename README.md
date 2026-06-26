# Project Harvest — Web-Based Image Grabber

> **WiTree Technology Solutions Pvt Ltd** — Engineering Training & Development

A locally-run web application that scans any public web page, displays all discovered images in a selectable grid, and downloads chosen images to a local folder.

---

## Quick Start (Windows)

Double-click **`run.bat`** — it installs dependencies and starts the server automatically.  
Then open **http://127.0.0.1:5000** in your browser.

## Manual Setup

### Prerequisites
- Python 3.9 or later

### Installation

```bash
# Clone the repository and enter the project folder
cd project_harvest

# Install dependencies
pip install -r requirements.txt
```

### Running — Development Mode

```bash
set HARVEST_DEV=true
python app.py
```

The app starts on `http://127.0.0.1:5000` with Flask debug mode and relaxed SSRF checks (allows localhost targets).

### Running — Production Mode

```bash
python app.py
```

Uses **Waitress** WSGI server. SSRF protection is fully active.

---

## Configuration

All settings are read from **environment variables** — no code changes needed.

| Variable | Default | Description |
|---|---|---|
| `HARVEST_HOST` | `127.0.0.1` | Bind address |
| `HARVEST_PORT` | `5000` | Port number |
| `HARVEST_DEV` | `false` | Enable dev mode (relaxes SSRF checks) |
| `HARVEST_IMAGE_FOLDER` | `images` | Folder name for saved images |
| `HARVEST_MAX_PAGE_SIZE` | `10485760` | Max page size in bytes (10 MB) |
| `HARVEST_MAX_IMAGE_SIZE` | `26214400` | Max image size in bytes (25 MB) |
| `HARVEST_MAX_GRAB_COUNT` | `100` | Max images per grab request |
| `HARVEST_TIMEOUT` | `15` | HTTP request timeout (seconds) |

---

## Running Tests

```bash
pytest test_app.py -v
```

All tests must pass before submission.

---

## Project Structure

| File | Purpose |
|---|---|
| `app.py` | Backend: Flask routes, validation, extraction, download logic |
| `index.html` | Frontend: single-page UI |
| `test_app.py` | Automated test suite |
| `requirements.txt` | Pinned Python dependencies |
| `run.bat` | Windows one-click launcher |
| `design_note.md` | Key design and security decisions |
| `README.md` | This file |
| `images/` | Created at runtime; holds downloaded images |

---

## Usage

1. Open the app in your browser.
2. Paste a public URL into the input field and click **Scan** (or press Enter).
3. Browse the image grid — click any image to select/deselect it.
4. Use **Select All**, **Clear**, or **Hide Small** to manage your selection.
5. Click **Grab Selected** to download chosen images to the `images/` folder.
6. Each image shows a success or failure status after the grab completes.

---

## Responsible Use

> **⚠️ Important:** This tool is for lawful, personal use only. Please:
> - Respect `robots.txt` directives and website terms of service.
> - Do not use this tool to download copyrighted images without permission.
> - Avoid scanning sites at high frequency — the tool sends requests at a reasonable pace.
