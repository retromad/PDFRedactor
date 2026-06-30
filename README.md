# PDFRedactor

A self-contained Windows desktop application that redacts sensitive keywords from PDF files — including text, scanned pages, and images embedded within pages — using keyword matching and Tesseract OCR.

## Screenshot

![PDFRedactor GUI](screenshot.png)

## Features

- **Keyword-based redaction** — build a keyword list (one per line or comma-separated) and redact every match across all pages
- **Editable keyword list** — type keywords directly, add them one at a time, or load and **save** a `.txt` keyword file for reuse
- **Add keywords from the preview** — drag-select any text in the PDF preview to add it straight to the keyword list
- **Live side-by-side preview** — view the Original and Redacted PDFs in the same window with:
  - Synchronized scrolling and page navigation between both panels
  - Continuous scroll that flips pages at the top/bottom edge
  - Zoom controls (25%–500%, or Fit-to-width) and a Go-to-page box
  - A **Pop Out** button to detach the previews into their own window — drag it to a second monitor
- **Full-page OCR redaction** — when enabled, OCRs the entire page (not just embedded images) so text baked into **headers, logos, screenshots, and scanned pages** is detected and redacted directly in the image pixels
- **Case-insensitive matching** — optional toggle to catch all capitalisation variants
- **Multi-file batch processing** — queue multiple PDFs; selecting a file shows its matching original/redacted pair
- **Progress tracking** — per-page and per-file progress bars with a live log showing redaction counts and OCR activity
- **Custom output folder** — save redacted files alongside the originals or to a chosen directory
- **No internet required** — fully offline after installation

## Installation (Windows 10/11)

1. Download **PDFRedactOCR_Setup.exe** from the [Releases](../../releases) page
2. Double-click to install — no administrator rights required
3. A **PDF Redactor** shortcut is added to your Desktop and Start Menu
4. Click the shortcut to launch

The installer bundles Python 3.11, PyMuPDF, pytesseract, Pillow, and Tesseract OCR with English language data. Nothing else needs to be installed.

## Installation (macOS)

1. Download **PDFRedactor.dmg** from the [Releases](../../releases) page
2. Open the `.dmg` and drag **PDF Redactor** into **Applications**
3. First launch: right-click the app → **Open** (the app is unsigned, so Gatekeeper asks once)

The `.app` bundles Python and all Python dependencies. **OCR** uses Tesseract from the system — install it once with:

```bash
brew install tesseract
```

Text redaction works without it; the OCR option is enabled automatically once Tesseract is present.

## Usage

1. **Keywords** — type terms directly into the keyword box (one per line), use the **Add Keyword** field, or click **Browse…** to load an existing `.txt` file. Click **Save** to write the list back to a file for reuse:
   ```
   John Smith
   john.smith@example.com
   555-1234
   ```
2. **PDF files** — click **Add PDFs…** to queue one or more PDF files. The first file is shown in the Original preview automatically
3. **Output folder** *(optional)* — by default redacted files are saved next to the originals with a `_redacted` suffix. Click **Choose Folder…** to save them elsewhere
4. **Options**
   - *Case-insensitive matching* — also matches `JOHN SMITH`, `john smith`, etc.
   - *OCR image pages* — OCRs each full page so text inside images, logos, headers, and scans is redacted (enabled by default; requires the bundled Tesseract)
5. Click **Start Redaction**

Redacted PDFs are saved as `<original_name>_redacted.pdf`. The log panel shows how many text and OCR redactions were applied per file.

### Preview controls

- **Show Original PDF / Show Redacted PDF** — open either preview on the right side of the window
- **Drag-select** text in a preview to add it to the keyword list
- **‹ Prev / Next ›** and **Go to page** — navigate pages; both panels stay in sync
- **Zoom −/+/Fit** — scale the page; the mouse wheel scrolls (and flips pages at the edges)
- **Pop Out** — move the previews into a separate, draggable window (e.g. a second monitor)

## Building the installers (from macOS)

Requirements: macOS with Homebrew.

**Windows installer** (`dist_windows/PDFRedactOCR_Setup.exe`):

```bash
brew install nsis
python3 build_windows_installer.py
```

The script downloads all dependencies automatically (Python runtime, wheels, Tesseract) and compiles a fully self-contained installer. `sevenzip` is installed via Homebrew automatically if not already present.

**macOS app + dmg** (`dist_macos/PDF Redactor.app`, `dist_macos/PDFRedactor.dmg`):

```bash
pip install pyinstaller
python3 build_macos_app.py
```

Produces a lightweight `.app`/`.dmg`. OCR relies on a system Tesseract (`brew install tesseract`).

## Keyword file format

```
# Lines starting with # are comments
First Last
email@domain.com
Account Number, SSN, Date of Birth
123-45-6789
```

## Dependencies (bundled — no separate install needed)

| Component | Version |
|---|---|
| Python | 3.11 |
| PyMuPDF | 1.27+ |
| pytesseract | 0.3.13 |
| Pillow | 12+ |
| Tesseract OCR | 5.4 (English) |

## License

MIT
