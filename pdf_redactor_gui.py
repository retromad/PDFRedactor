#!/usr/bin/env python3
"""
PDF Redactor GUI — single-file portable application.
Build into a Windows .exe or macOS .app with build.py.
"""

from __future__ import annotations

import queue
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except ImportError:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing dependency",
            "PyMuPDF is not installed.\n\nRun:  pip install pymupdf",
        )
        root.destroy()
    except Exception:
        print("Error: PyMuPDF is not installed. Run: pip install pymupdf",
              file=sys.stderr)
    sys.exit(1)

# Optional OCR dependencies
try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False


# ── Tesseract detection ───────────────────────────────────────────────────────

_TESSERACT_CMD: Optional[str] = None
_TESSERACT_CONFIG: str = ""   # extra CLI flags passed to every pytesseract call

def _find_tesseract() -> Optional[str]:
    """
    Look for tesseract.exe in this order:
      1. Bundled alongside this script (installer puts it in tesseract/ next to us)
      2. System PATH
      3. Common Windows installation paths
    """
    # Derive the directory this script lives in — works both from shortcut
    # (sys.argv[0] = full path to .py) and direct execution (__file__)
    candidates = []
    if sys.argv[0]:
        candidates.append(Path(sys.argv[0]).resolve().parent)
    candidates.append(Path(__file__).resolve().parent)

    for script_dir in candidates:
        bundled = script_dir / "tesseract" / "tesseract.exe"
        if bundled.exists():
            return str(bundled)

    if shutil.which("tesseract"):
        return shutil.which("tesseract")

    for p in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]:
        if Path(p).exists():
            return p

    return None


def _init_tesseract() -> tuple:
    """
    Return (ready: bool, status_msg: str).
    Configures pytesseract and the environment so the bundled tesseract.exe
    and its sibling DLLs are found when running as a subprocess.
    """
    import os

    global _TESSERACT_CMD
    if not _OCR_AVAILABLE:
        return False, "pytesseract / Pillow not installed"
    if _TESSERACT_CMD is not None:
        return True, f"Tesseract ready: {_TESSERACT_CMD}"

    cmd = _find_tesseract()
    if cmd is None:
        # Build a helpful message showing where we looked
        searched = []
        for base in ([Path(sys.argv[0]).resolve().parent] if sys.argv[0] else []) + [Path(__file__).resolve().parent]:
            searched.append(str(base / "tesseract" / "tesseract.exe"))
        return False, "tesseract.exe not found.\nSearched:\n" + "\n".join(searched)

    global _TESSERACT_CONFIG

    tess_dir  = Path(cmd).parent
    tess_dir_s = str(tess_dir)

    # Add tesseract dir to PATH so Windows finds all sibling DLLs
    os.environ["PATH"] = tess_dir_s + os.pathsep + os.environ.get("PATH", "")

    # Tesseract 5.x appends "/lang.traineddata" directly to TESSDATA_PREFIX,
    # so point it at the tessdata/ folder itself, not its parent.
    # Do NOT use --tessdata-dir in the config string — shlex.split on Windows
    # passes the surrounding quotes as literal characters to the subprocess.
    tessdata = tess_dir / "tessdata"
    if tessdata.exists():
        os.environ["TESSDATA_PREFIX"] = str(tessdata)
    _TESSERACT_CONFIG = ""  # TESSDATA_PREFIX handles it; no --tessdata-dir needed

    pytesseract.pytesseract.tesseract_cmd = cmd

    try:
        pytesseract.get_tesseract_version()
        _TESSERACT_CMD = cmd
        return True, "Tesseract ready"
    except Exception as exc:
        return False, f"Tesseract found at {cmd}\nbut failed to run: {exc}"


# ── Redaction engine ──────────────────────────────────────────────────────────

def load_keywords(path: Path) -> List[str]:
    keywords = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for term in line.split(","):
                term = term.strip()
                if term:
                    keywords.append(term)
    return keywords


def _expand_keywords(keywords: List[str], case_insensitive: bool) -> List[str]:
    if not case_insensitive:
        return keywords
    expanded = set()
    for kw in keywords:
        expanded.update([kw, kw.lower(), kw.upper(), kw.title()])
    return list(expanded)


def _is_image_only(page) -> bool:
    """True when the page has no extractable text (scanned / pasted image)."""
    return len(page.get_text().strip()) == 0


def _get_embedded_image_rects(page) -> List[fitz.Rect]:
    """Return bounding boxes of all embedded images on the page."""
    rects = []
    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox:
            continue
        r = fitz.Rect(bbox)
        if not r.is_empty and r.get_area() > 100:   # ignore tiny decorative images
            rects.append(r)
    return rects


def _ocr_region(
    page,
    clip: fitz.Rect,
    keywords: List[str],
    case_insensitive: bool,
    zoom: float = 300 / 72,
) -> int:
    """
    OCR a rectangular region of the page (in PDF coordinates) and add redact
    annotations for every keyword match.  Returns the number of redactions added.
    """
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=clip)
    if pix.width == 0 or pix.height == 0:
        return 0

    img  = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    data = pytesseract.image_to_data(
        img, output_type=pytesseract.Output.DICT, lang="eng",
        config=_TESSERACT_CONFIG,
    )

    # Build (word, pdf_rect) pairs, converting pixel coords back to PDF space
    word_boxes: List[Tuple[str, fitz.Rect]] = []
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        if not word:
            continue
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = 0
        if conf < 20:
            continue
        # pixel → PDF: offset by the clip origin
        x0 = clip.x0 + data["left"][i] / zoom
        y0 = clip.y0 + data["top"][i]  / zoom
        x1 = clip.x0 + (data["left"][i] + data["width"][i])  / zoom
        y1 = clip.y0 + (data["top"][i]  + data["height"][i]) / zoom
        word_boxes.append((word, fitz.Rect(x0, y0, x1, y1)))

    hits = 0
    for kw in keywords:
        kw_tokens = kw.split()
        n = len(kw_tokens)
        for i in range(len(word_boxes) - n + 1):
            chunk = word_boxes[i : i + n]
            chunk_text = " ".join(w for w, _ in chunk)
            a = chunk_text.lower() if case_insensitive else chunk_text
            b = kw.lower()         if case_insensitive else kw
            if a == b:
                union_rect = chunk[0][1]
                for _, r in chunk[1:]:
                    union_rect = union_rect | r
                page.add_redact_annot(union_rect, fill=(0, 0, 0))
                hits += 1

    return hits


def redact_pdf(
    pdf_path: Path,
    keywords: List[str],
    output_path: Path,
    case_insensitive: bool,
    use_ocr: bool,
    progress_cb,   # signature: (page_num, total, msg="")
    cancel_event,
) -> Tuple[int, int]:
    """
    Returns (text_redactions, ocr_redactions).

    For every page:
      - Text search runs on selectable text (always).
      - If OCR is enabled:
          * Image-only pages  → OCR the full page.
          * Mixed pages       → OCR each embedded image's bounding box.
    """
    text_keywords = _expand_keywords(keywords, case_insensitive)

    doc = fitz.open(pdf_path)
    total_pages     = len(doc)
    text_redactions = 0
    ocr_redactions  = 0

    for page_num, page in enumerate(doc, start=1):
        if cancel_event.is_set():
            doc.close()
            raise InterruptedError("Cancelled by user")

        progress_cb(page_num, total_pages, "")

        text_hits = 0
        ocr_hits  = 0

        if _is_image_only(page):
            if use_ocr:
                # Whole page is an image — OCR the entire page rect
                ocr_hits = _ocr_region(page, page.rect, keywords, case_insensitive)
                progress_cb(page_num, total_pages, f"p{page_num}: full-page OCR → {ocr_hits} hit(s)")
        else:
            # Selectable text — fast search
            for keyword in text_keywords:
                for rect in page.search_for(keyword):
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    text_hits += 1

            if use_ocr:
                # Also OCR any embedded images on this page
                img_rects = _get_embedded_image_rects(page)
                for img_rect in img_rects:
                    ocr_hits += _ocr_region(
                        page, img_rect, keywords, case_insensitive
                    )
                if img_rects:
                    progress_cb(page_num, total_pages,
                                f"p{page_num}: {len(img_rects)} embedded image(s) OCR'd → {ocr_hits} hit(s)")

        if text_hits or ocr_hits:
            # Use PIXELS mode when image content needs to be painted over
            img_mode = (
                fitz.PDF_REDACT_IMAGE_PIXELS if ocr_hits
                else fitz.PDF_REDACT_IMAGE_NONE
            )
            page.apply_redactions(images=img_mode)
            text_redactions += text_hits
            ocr_redactions  += ocr_hits

    total = text_redactions + ocr_redactions
    if total:
        doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return text_redactions, ocr_redactions


# ── GUI ───────────────────────────────────────────────────────────────────────

ACCENT   = "#1565C0"
ERR_CLR  = "#CC0000"
OK_CLR   = "#006600"
WARN_CLR = "#CC6600"
INFO_CLR = "#1565C0"


class PDFRedactorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Redactor")
        self.resizable(True, True)
        self.minsize(700, 660)

        self.keywords_path: Optional[Path] = None
        self.pdf_paths: List[Path] = []
        self.output_dir: Optional[Path] = None
        self._last_output_dir: Optional[Path] = None
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()

        self._tesseract_ready, self._tesseract_status = _init_tesseract()

        self._build_ui()
        self._poll_queue()
        if not self._tesseract_ready:
            self._log(f"Tesseract: {self._tesseract_status}", "err")

        self.geometry("720x720")
        self.update_idletasks()
        self.update()

        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication, NSApp  # type: ignore
                NSApplication.sharedApplication()
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                import subprocess
                subprocess.Popen(
                    ["osascript", "-e",
                     'tell application "PDFRedactor" to activate'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

        self.lift()
        self.focus_force()

    def _build_ui(self):
        pad = dict(padx=12, pady=6)

        # ── Section: Keywords ─────────────────────────────────────────────────
        kw_lf = ttk.LabelFrame(self, text="1  Redaction Keywords File", padding=8)
        kw_lf.pack(fill="x", **pad)

        kw_row = ttk.Frame(kw_lf)
        kw_row.pack(fill="x")
        self.kw_label = ttk.Label(kw_row, text="No file selected", foreground="gray")
        self.kw_label.pack(side="left", fill="x", expand=True)
        ttk.Button(kw_row, text="Browse…", command=self._browse_keywords).pack(side="left", padx=(6, 0))
        ttk.Button(kw_row, text="Clear",   command=self._clear_keywords).pack(side="left", padx=(4, 0))

        self.kw_preview = tk.Text(kw_lf, height=4, font=("Courier", 10),
                                   state="disabled", wrap="word",
                                   relief="sunken", bd=1)
        self.kw_preview.pack(fill="x", pady=(6, 0))

        # ── Section: PDF Files ────────────────────────────────────────────────
        pdf_lf = ttk.LabelFrame(self, text="2  PDF Files to Redact", padding=8)
        pdf_lf.pack(fill="x", **pad)

        pdf_btn_row = ttk.Frame(pdf_lf)
        pdf_btn_row.pack(fill="x")
        ttk.Button(pdf_btn_row, text="Add PDFs…",  command=self._browse_pdfs).pack(side="left")
        ttk.Button(pdf_btn_row, text="Clear List", command=self._clear_pdfs).pack(side="left", padx=(6, 0))

        list_frame = ttk.Frame(pdf_lf)
        list_frame.pack(fill="x", pady=(6, 0))
        self.pdf_listbox = tk.Listbox(list_frame, height=5, font=("Courier", 10),
                                       selectmode="extended", relief="sunken", bd=1)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.pdf_listbox.yview)
        self.pdf_listbox.configure(yscrollcommand=sb.set)
        self.pdf_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ── Section: Output Folder ────────────────────────────────────────────
        out_lf = ttk.LabelFrame(self, text="3  Output Folder", padding=8)
        out_lf.pack(fill="x", **pad)

        out_row = ttk.Frame(out_lf)
        out_row.pack(fill="x")
        self.out_label = ttk.Label(out_row,
                                    text="Same folder as each PDF (default)",
                                    foreground="gray")
        self.out_label.pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Choose Folder…", command=self._browse_output).pack(side="left", padx=(6, 0))
        ttk.Button(out_row, text="Reset",          command=self._clear_output).pack(side="left", padx=(4, 0))

        # ── Options ───────────────────────────────────────────────────────────
        opt_row = ttk.Frame(self)
        opt_row.pack(fill="x", padx=12, pady=(2, 2))
        self.case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_row, text="Case-insensitive matching",
                        variable=self.case_var).pack(side="left")

        # OCR option
        ocr_row = ttk.Frame(self)
        ocr_row.pack(fill="x", padx=12, pady=(0, 4))
        self.ocr_var = tk.BooleanVar(value=self._tesseract_ready)
        self.ocr_cb  = ttk.Checkbutton(
            ocr_row,
            text="OCR image pages (full-page scans and embedded images)",
            variable=self.ocr_var,
        )
        self.ocr_cb.pack(side="left")

        if not self._tesseract_ready:
            self.ocr_var.set(False)
            self.ocr_cb.configure(state="disabled")
            # Show first line of status inline; full detail goes to the log on startup
            short = self._tesseract_status.splitlines()[0]
            ttk.Label(
                ocr_row,
                text=f"  ✗ {short}",
                foreground=ERR_CLR,
            ).pack(side="left", padx=(8, 0))
        else:
            ttk.Label(ocr_row, text="  ✓ Tesseract ready",
                      foreground=OK_CLR).pack(side="left", padx=(8, 0))

        # ── Action Buttons ────────────────────────────────────────────────────
        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=12, pady=(0, 6))
        self.run_btn = ttk.Button(btn_row, text="Start Redaction",
                                   command=self._start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btn_row, text="Cancel",
                                      command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.open_out_btn = ttk.Button(btn_row, text="Open Output Folder",
                                        command=self._open_output_folder,
                                        state="disabled")
        self.open_out_btn.pack(side="right")

        # ── Progress ──────────────────────────────────────────────────────────
        prog_lf = ttk.LabelFrame(self, text="Progress", padding=8)
        prog_lf.pack(fill="both", expand=True, **pad)

        self.file_label = ttk.Label(prog_lf, text="")
        self.file_label.pack(anchor="w")

        ttk.Label(prog_lf, text="Page progress:").pack(anchor="w", pady=(6, 0))
        self.page_bar = ttk.Progressbar(prog_lf, orient="horizontal",
                                         mode="determinate", length=400)
        self.page_bar.pack(fill="x")

        ttk.Label(prog_lf, text="File progress:").pack(anchor="w", pady=(6, 0))
        self.file_bar = ttk.Progressbar(prog_lf, orient="horizontal",
                                         mode="determinate", length=400)
        self.file_bar.pack(fill="x")

        ttk.Label(prog_lf, text="Log:").pack(anchor="w", pady=(8, 0))
        log_frame = ttk.Frame(prog_lf)
        log_frame.pack(fill="both", expand=True, pady=(2, 0))
        self.log = tk.Text(log_frame, height=8, font=("Courier", 10),
                           state="disabled", wrap="word", relief="sunken", bd=1)
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        self.log.tag_configure("ok",   foreground=OK_CLR)
        self.log.tag_configure("err",  foreground=ERR_CLR)
        self.log.tag_configure("warn", foreground=WARN_CLR)
        self.log.tag_configure("info", foreground=INFO_CLR)

    # ── file selection ────────────────────────────────────────────────────────

    def _browse_keywords(self):
        path = filedialog.askopenfilename(
            title="Select keywords file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        self.keywords_path = Path(path)
        self.kw_label.configure(text=self.keywords_path.name, foreground="black")
        try:
            kws = load_keywords(self.keywords_path)
            preview = "\n".join(kws[:50])
            if len(kws) > 50:
                preview += f"\n... and {len(kws) - 50} more"
            self._set_text(self.kw_preview, preview)
            self._log(f"Loaded {len(kws)} keyword(s) from {self.keywords_path.name}", "info")
        except Exception as exc:
            self._show_error("Failed to read keywords file", exc)

    def _clear_keywords(self):
        self.keywords_path = None
        self.kw_label.configure(text="No file selected", foreground="gray")
        self._set_text(self.kw_preview, "")

    def _browse_pdfs(self):
        paths = filedialog.askopenfilenames(
            title="Select PDF files",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        added = 0
        for p in paths:
            path = Path(p)
            if path not in self.pdf_paths:
                self.pdf_paths.append(path)
                self.pdf_listbox.insert("end", path.name)
                added += 1
        if added:
            self._log(f"Added {added} PDF(s). Total queued: {len(self.pdf_paths)}", "info")

    def _clear_pdfs(self):
        self.pdf_paths.clear()
        self.pdf_listbox.delete(0, "end")

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir = Path(path)
            self.out_label.configure(text=str(self.output_dir), foreground="black")

    def _clear_output(self):
        self.output_dir = None
        self.out_label.configure(
            text="Same folder as each PDF (default)", foreground="gray")

    # ── redaction worker ──────────────────────────────────────────────────────

    def _start(self):
        if not self.keywords_path:
            self._show_error("No keywords file", "Please select a keywords file first.")
            return
        if not self.pdf_paths:
            self._show_error("No PDFs selected", "Please add at least one PDF file.")
            return
        try:
            keywords = load_keywords(self.keywords_path)
        except Exception as exc:
            self._show_error("Failed to load keywords", exc)
            return
        if not keywords:
            self._show_error("Empty keywords file",
                             "The keywords file contains no terms to redact.")
            return

        self.cancel_event.clear()
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_out_btn.configure(state="disabled")
        self._log("-" * 60, "info")
        self._log(f"Starting: {len(self.pdf_paths)} file(s), {len(keywords)} keyword(s)...", "info")
        if self.ocr_var.get():
            self._log("OCR mode enabled for image-only pages.", "info")
        self.file_bar["maximum"] = len(self.pdf_paths)
        self.file_bar["value"] = 0

        threading.Thread(
            target=self._worker,
            args=(keywords, list(self.pdf_paths), self.output_dir,
                  self.case_var.get(), self.ocr_var.get(), self.cancel_event),
            daemon=True,
        ).start()

    def _worker(self, keywords, pdf_paths, output_dir, case_insensitive,
                use_ocr, cancel_event):
        grand_text = 0
        grand_ocr  = 0
        completed  = 0
        errors     = 0
        last_output_dir: Optional[Path] = None

        for pdf_path in pdf_paths:
            if cancel_event.is_set():
                self._queue("log", ("Cancelled.", "warn"))
                break

            self._queue("file_label", f"Processing: {pdf_path.name}")

            if output_dir:
                out_path = output_dir / (pdf_path.stem + "_redacted.pdf")
                last_output_dir = output_dir
            else:
                out_path = pdf_path.with_name(pdf_path.stem + "_redacted.pdf")
                last_output_dir = pdf_path.parent

            def page_cb(page_num, total, msg=""):
                self._queue("page_progress", (page_num, total))
                if msg:
                    self._queue("log", (msg, "info"))

            try:
                txt, ocr = redact_pdf(
                    pdf_path, keywords, out_path,
                    case_insensitive, use_ocr, page_cb, cancel_event,
                )
                grand_text += txt
                grand_ocr  += ocr
                completed  += 1
                total = txt + ocr
                if total:
                    parts = []
                    if txt: parts.append(f"{txt} text")
                    if ocr: parts.append(f"{ocr} OCR")
                    self._queue("log", (
                        f"OK  {pdf_path.name} -> {out_path.name}  "
                        f"({', '.join(parts)} redaction(s))", "ok"))
                else:
                    self._queue("log", (
                        f"--  {pdf_path.name}: no matches, file not saved", "warn"))
            except InterruptedError:
                self._queue("log", ("Cancelled by user.", "warn"))
                break
            except Exception as exc:
                errors += 1
                self._queue("log", (f"ERR  {pdf_path.name}: {exc}", "err"))

            self._queue("file_progress", completed)

        parts = [f"{completed} file(s) processed"]
        if grand_text: parts.append(f"{grand_text} text redaction(s)")
        if grand_ocr:  parts.append(f"{grand_ocr} OCR redaction(s)")
        if errors:     parts.append(f"{errors} error(s)")
        tag = "ok" if not errors else "warn"
        self._queue("log", ("Done. " + ", ".join(parts) + ".", tag))
        self._queue("done", last_output_dir)

    # ── queue polling ─────────────────────────────────────────────────────────

    def _queue(self, kind, data=None):
        self.msg_queue.put((kind, data))

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.msg_queue.get_nowait()
                if kind == "log":
                    text, tag = data
                    self._log(text, tag)
                elif kind == "file_label":
                    self.file_label.configure(text=data)
                elif kind == "page_progress":
                    page_num, total = data
                    self.page_bar["maximum"] = total
                    self.page_bar["value"] = page_num
                    pct = int(page_num / total * 100)
                    base = self.file_label.cget("text").split("  --")[0]
                    self.file_label.configure(
                        text=f"{base}  --  page {page_num}/{total} ({pct}%)")
                elif kind == "file_progress":
                    self.file_bar["value"] = data
                elif kind == "done":
                    self._on_done(data)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    def _on_done(self, last_output_dir: Optional[Path]):
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.file_label.configure(text="")
        self.page_bar["value"] = 0
        if last_output_dir and last_output_dir.is_dir():
            self._last_output_dir = last_output_dir
            self.open_out_btn.configure(state="normal")

    def _cancel(self):
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self._log("Cancel requested...", "warn")

    def _open_output_folder(self):
        d = self._last_output_dir
        if d and d.is_dir():
            import subprocess
            if sys.platform == "win32":
                import os; os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])

    # ── helpers ───────────────────────────────────────────────────────────────

    def _log(self, text: str, tag: str = ""):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_text(self, widget: tk.Text, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _show_error(self, title: str, detail):
        msg = str(detail)
        self._log(f"ERROR -- {title}: {msg}", "err")
        messagebox.showerror(title, msg)


if __name__ == "__main__":
    app = PDFRedactorApp()
    app.mainloop()
