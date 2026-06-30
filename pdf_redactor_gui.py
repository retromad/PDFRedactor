#!/usr/bin/env python3
"""
PDF Redactor GUI — single-file portable application.
Build into a Windows .exe or macOS .app with build.py.
"""

from __future__ import annotations

import base64
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

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
# PSM 11 = "sparse text: find as much text as possible in no particular order".
# Essential for stylized text on busy image backgrounds (logos, screenshots,
# headers) that the default PSM 3 segmentation misses entirely.
_OCR_PSM: str = "11"

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
    ocr_config = (_TESSERACT_CONFIG + f" --psm {_OCR_PSM}").strip()
    data = pytesseract.image_to_data(
        img, output_type=pytesseract.Output.DICT, lang="eng",
        config=ocr_config,
    )

    # Build (word, pdf_rect) pairs, converting pixel coords back to PDF space.
    # Use the pixmap's actual device origin (pix.x, pix.y) rather than clip.x0/y0:
    # when the image bbox extends past a page edge, PyMuPDF clamps the rendered
    # region to the page, so the pixmap origin differs from the requested clip.
    ox, oy = pix.x, pix.y
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
        # pixel → PDF: offset by the pixmap's device origin, then unscale
        x0 = (ox + data["left"][i]) / zoom
        y0 = (oy + data["top"][i])  / zoom
        x1 = (ox + data["left"][i] + data["width"][i])  / zoom
        y1 = (oy + data["top"][i]  + data["height"][i]) / zoom
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

        # Always run a fast, exact search over any selectable text.
        for keyword in text_keywords:
            for rect in page.search_for(keyword):
                page.add_redact_annot(rect, fill=(0, 0, 0))
                text_hits += 1

        # When OCR is enabled, OCR the WHOLE page (not just embedded-image
        # bounding boxes). This catches text baked into header banners, logos,
        # screenshots and other graphics that get_image_info() may not report.
        if use_ocr:
            ocr_hits = _ocr_region(page, page.rect, keywords, case_insensitive)
            if ocr_hits:
                progress_cb(page_num, total_pages,
                            f"p{page_num}: full-page OCR → {ocr_hits} hit(s)")

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
        self.minsize(700, 500)

        self.keywords_path: Optional[Path] = None
        self.pdf_paths: List[Path] = []
        self.output_dir: Optional[Path] = None
        self._last_output_dir: Optional[Path] = None
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()

        # Preview state — keyed by "orig" and "redact"
        self._prev_docs:   Dict[str, Optional[object]] = {"orig": None, "redact": None}
        self._prev_pages:  Dict[str, int]              = {"orig": 0,    "redact": 0}
        self._prev_photos: Dict[str, Optional[object]] = {"orig": None, "redact": None}
        self._prev_canvas: Dict[str, Optional[tk.Canvas]] = {"orig": None, "redact": None}
        self._prev_label:  Dict[str, Optional[ttk.Label]] = {"orig": None, "redact": None}
        self._prev_btn:    Dict[str, Dict[str, ttk.Button]] = {"orig": {}, "redact": {}}
        self._prev_panel:  Dict[str, Optional[tk.Frame]]   = {"orig": None, "redact": None}
        self._prev_visible: Dict[str, bool] = {"orig": False, "redact": False}
        self._show_btn:    Dict[str, Optional[ttk.Button]] = {"orig": None, "redact": None}
        self._hover_canvas_key: Optional[str] = None  # which canvas the mouse is over
        self._flip_cooldown_until: float = 0.0  # suppress momentum after a page flip
        self._redacted_map: Dict[Path, Path] = {}  # original PDF -> redacted output
        self._prev_zoom:   Dict[str, float] = {"orig": 1.0, "redact": 1.0}
        self._sel_start:   Dict[str, Optional[tuple]] = {"orig": None, "redact": None}
        self._sel_rect_id: Dict[str, Optional[int]]   = {"orig": None, "redact": None}
        # User zoom multiplier on top of fit-to-width (1.0 = fit width = 100%)
        self._zoom_factor: Dict[str, float] = {"orig": 1.0, "redact": 1.0}
        self._zoom_lbl:    Dict[str, Optional[tk.Label]] = {"orig": None, "redact": None}
        self._goto_entry:  Dict[str, Optional[tk.Entry]] = {"orig": None, "redact": None}
        # Pop-out preview window state
        self._popped_out:  bool = False
        self._preview_win: Optional[tk.Toplevel] = None
        self._popout_btn:  Optional[ttk.Button] = None

        self._tesseract_ready, self._tesseract_status = _init_tesseract()

        try:
            self._build_ui()
        except Exception as _e:
            import traceback; traceback.print_exc()
            raise
        self._poll_queue()
        if not self._tesseract_ready:
            self._log(f"Tesseract: {self._tesseract_status}", "err")

        self.geometry("1400x860")
        self.update_idletasks()
        self.update()
        self.lift()
        self.focus_force()

        # Fallback handlers so scrolling still works if the pointer is over a
        # sub-widget; they redirect to whichever canvas is hovered.
        self.bind_all("<MouseWheel>", self._on_root_wheel)
        self.bind_all("<Button-4>",   self._on_root_wheel)
        self.bind_all("<Button-5>",   self._on_root_wheel)
        self.bind_all("<TouchpadScroll>", self._on_root_touchpad)

    def _build_ui(self):
        # Two-column layout: controls left, PDF previews right.
        self.grid_columnconfigure(0, weight=0)   # controls — fixed width
        self.grid_columnconfigure(1, weight=1)   # previews — expand
        self.grid_rowconfigure(0, weight=1)      # single row fills height

        # ── Left column: all controls ─────────────────────────────────────────
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        # Keywords
        kw_lf = ttk.LabelFrame(left, text="1  Redaction Keywords", padding=8)
        kw_lf.pack(fill="x", pady=(0, 6))
        kw_row = ttk.Frame(kw_lf)
        kw_row.pack(fill="x")
        self.kw_label = ttk.Label(kw_row, text="No file selected", foreground="gray")
        self.kw_label.pack(side="left", fill="x", expand=True)
        ttk.Button(kw_row, text="Browse…", command=self._browse_keywords).pack(side="left", padx=(6, 0))
        ttk.Button(kw_row, text="Save",    command=self._save_keywords).pack(side="left", padx=(4, 0))
        ttk.Button(kw_row, text="Clear",   command=self._clear_keywords).pack(side="left", padx=(4, 0))

        # Editable keyword list — one keyword per line
        self.kw_preview = tk.Text(kw_lf, height=6, font=("Courier", 10),
                                  wrap="word", relief="sunken", bd=1,
                                  undo=True)
        self.kw_preview.pack(fill="x", pady=(6, 0))

        # Add-a-keyword row
        add_row = ttk.Frame(kw_lf)
        add_row.pack(fill="x", pady=(4, 0))
        self.kw_entry = ttk.Entry(add_row)
        self.kw_entry.pack(side="left", fill="x", expand=True)
        self.kw_entry.bind("<Return>", lambda e: self._add_keyword())
        ttk.Button(add_row, text="Add Keyword", command=self._add_keyword).pack(side="left", padx=(6, 0))

        # PDF Files
        pdf_lf = ttk.LabelFrame(left, text="2  PDF Files to Redact", padding=8)
        pdf_lf.pack(fill="x", pady=(0, 6))
        pdf_btn_row = ttk.Frame(pdf_lf)
        pdf_btn_row.pack(fill="x")
        ttk.Button(pdf_btn_row, text="Add PDFs…",  command=self._browse_pdfs).pack(side="left")
        ttk.Button(pdf_btn_row, text="Clear List", command=self._clear_pdfs).pack(side="left", padx=(6, 0))
        list_frame = ttk.Frame(pdf_lf)
        list_frame.pack(fill="x", pady=(6, 0))
        self.pdf_listbox = tk.Listbox(list_frame, height=4, font=("Courier", 10),
                                      selectmode="extended", relief="sunken", bd=1)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.pdf_listbox.yview)
        self.pdf_listbox.configure(yscrollcommand=sb.set)
        self.pdf_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.pdf_listbox.bind("<<ListboxSelect>>", self._on_listbox_select)

        # Output Folder
        out_lf = ttk.LabelFrame(left, text="3  Output Folder", padding=8)
        out_lf.pack(fill="x", pady=(0, 6))
        out_row = ttk.Frame(out_lf)
        out_row.pack(fill="x")
        self.out_label = ttk.Label(out_row, text="Same folder as each PDF (default)", foreground="gray")
        self.out_label.pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Choose Folder…", command=self._browse_output).pack(side="left", padx=(6, 0))
        ttk.Button(out_row, text="Reset",          command=self._clear_output).pack(side="left", padx=(4, 0))

        # Options
        opt_row = ttk.Frame(left)
        opt_row.pack(fill="x", pady=(0, 2))
        self.case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_row, text="Case-insensitive matching",
                        variable=self.case_var).pack(side="left")

        ocr_row = ttk.Frame(left)
        ocr_row.pack(fill="x", pady=(0, 6))
        self.ocr_var = tk.BooleanVar(value=self._tesseract_ready)
        self.ocr_cb  = ttk.Checkbutton(ocr_row,
                                        text="OCR image pages",
                                        variable=self.ocr_var)
        self.ocr_cb.pack(side="left")
        if not self._tesseract_ready:
            self.ocr_var.set(False)
            self.ocr_cb.configure(state="disabled")
            ttk.Label(ocr_row, text="  ✗ Tesseract not found",
                      foreground=ERR_CLR).pack(side="left", padx=(4, 0))
        else:
            ttk.Label(ocr_row, text="  ✓ Tesseract ready",
                      foreground=OK_CLR).pack(side="left", padx=(4, 0))

        # Action buttons
        btn_row = ttk.Frame(left)
        btn_row.pack(fill="x", pady=(0, 6))
        self.run_btn = ttk.Button(btn_row, text="Start Redaction", command=self._start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btn_row, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.open_out_btn = ttk.Button(btn_row, text="Open Output Folder",
                                       command=self._open_output_folder, state="disabled")
        self.open_out_btn.pack(side="right")

        # Progress
        prog_lf = ttk.LabelFrame(left, text="Progress", padding=8)
        prog_lf.pack(fill="x", pady=(0, 6))
        self.file_label = ttk.Label(prog_lf, text="")
        self.file_label.pack(anchor="w")
        ttk.Label(prog_lf, text="Page:").pack(anchor="w", pady=(4, 0))
        self.page_bar = ttk.Progressbar(prog_lf, orient="horizontal", mode="determinate")
        self.page_bar.pack(fill="x")
        ttk.Label(prog_lf, text="File:").pack(anchor="w", pady=(4, 0))
        self.file_bar = ttk.Progressbar(prog_lf, orient="horizontal", mode="determinate")
        self.file_bar.pack(fill="x")
        ttk.Label(prog_lf, text="Log:").pack(anchor="w", pady=(6, 0))
        log_frame = ttk.Frame(prog_lf)
        log_frame.pack(fill="x", pady=(2, 0))
        self.log = tk.Text(log_frame, height=6, width=38, font=("Courier", 10),
                           state="disabled", wrap="word", relief="sunken", bd=1)
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")
        self.log.tag_configure("ok",   foreground=OK_CLR)
        self.log.tag_configure("err",  foreground=ERR_CLR)
        self.log.tag_configure("warn", foreground=WARN_CLR)
        self.log.tag_configure("info", foreground=INFO_CLR)

        # ── Right column: preview toolbar + preview panels ────────────────────
        self._right = ttk.Frame(self)
        self._right.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)

        # Toolbar across the top of the preview area
        toolbar = ttk.Frame(self._right)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Label(toolbar, text="PDF Preview",
                  font=("TkDefaultFont", 11, "bold")).pack(side="left")
        # Buttons aligned to the top-right
        self._popout_btn = ttk.Button(toolbar, text="Pop Out",
                                      command=self._toggle_popout)
        self._popout_btn.pack(side="right")
        redact_btn = ttk.Button(toolbar, text="Show Redacted PDF",
                                command=lambda: self._toggle_panel("redact"))
        redact_btn.pack(side="right", padx=(0, 6))
        self._show_btn["redact"] = redact_btn
        orig_btn = ttk.Button(toolbar, text="Show Original PDF",
                              command=lambda: self._toggle_panel("orig"))
        orig_btn.pack(side="right", padx=(0, 6))
        self._show_btn["orig"] = orig_btn

        # Preview panels (vertical PanedWindow) fill the rest of the column
        self._make_preview_area(self._right)
        self._preview_area.pack(fill="both", expand=True)

    def _make_preview_area(self, master):
        """Create the preview PanedWindow under the given master widget."""
        self._preview_area = tk.PanedWindow(master, orient="vertical",
                                            sashrelief="raised", sashwidth=5,
                                            bg="#aaaaaa")

    def _build_preview_panel(self, key: str, title: str) -> tk.Frame:
        """Build a self-contained preview panel for the right PanedWindow."""
        panel = tk.Frame(self._preview_area, relief="flat", bd=0)

        # Header
        header = tk.Frame(panel, bg="#1565C0")
        header.pack(fill="x")
        tk.Label(header, text=title, bg="#1565C0", fg="white",
                 font=("TkDefaultFont", 10, "bold"), padx=8, pady=4).pack(side="left")
        ttk.Button(header, text="Close", width=6,
                   command=lambda: self._toggle_panel(key)).pack(side="right", padx=4, pady=3)

        # Page navigation (ttk buttons render as proper, readable native buttons
        # on macOS aqua, where tk.Button ignores bg/fg colors).
        nav = ttk.Frame(panel)
        nav.pack(fill="x", pady=(2, 0))
        prev_btn = ttk.Button(nav, text="‹ Prev", width=8,
                              command=lambda: self._prev_page(key))
        prev_btn.pack(side="left", padx=(4, 0))
        page_lbl = ttk.Label(nav, text="No document loaded", anchor="center")
        page_lbl.pack(side="left", fill="x", expand=True)
        next_btn = ttk.Button(nav, text="Next ›", width=8,
                              command=lambda: self._next_page(key))
        next_btn.pack(side="right", padx=(0, 4))
        prev_btn.configure(state="disabled")
        next_btn.configure(state="disabled")

        self._prev_label[key] = page_lbl
        self._prev_btn[key] = {"prev": prev_btn, "next": next_btn}

        # Second nav row: zoom controls + go-to-page
        nav2 = ttk.Frame(panel)
        nav2.pack(fill="x", pady=(2, 2))

        ttk.Label(nav2, text="Zoom:").pack(side="left", padx=(4, 2))
        ttk.Button(nav2, text="−", width=3,
                   command=lambda: self._zoom(key, 1 / 1.25)).pack(side="left")
        zoom_lbl = ttk.Label(nav2, text="100%", width=5, anchor="center")
        zoom_lbl.pack(side="left")
        ttk.Button(nav2, text="+", width=3,
                   command=lambda: self._zoom(key, 1.25)).pack(side="left")
        ttk.Button(nav2, text="Fit", width=4,
                   command=lambda: self._zoom(key, None)).pack(side="left", padx=(4, 0))
        self._zoom_lbl[key] = zoom_lbl

        # Go-to-page
        ttk.Label(nav2, text="Go to page:").pack(side="left", padx=(12, 2))
        goto = ttk.Entry(nav2, width=5)
        goto.pack(side="left")
        goto.bind("<Return>", lambda e, k=key: self._goto_page(k))
        ttk.Button(nav2, text="Go", width=4,
                   command=lambda: self._goto_page(key)).pack(side="left", padx=(4, 0))
        self._goto_entry[key] = goto

        # Scrollable canvas
        canvas_frame = tk.Frame(panel)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg="#606060", highlightthickness=0)
        vsb = ttk.Scrollbar(canvas_frame, orient="vertical",   command=canvas.yview)
        hsb = ttk.Scrollbar(canvas_frame, orient="horizontal", command=canvas.xview)
        def yscroll_sync(first, last, k=key):
            vsb.set(first, last)
            other = "redact" if k == "orig" else "orig"
            oc = self._prev_canvas.get(other)
            if oc and self._prev_visible.get(other):
                oc.yview_moveto(first)
        canvas.configure(yscrollcommand=yscroll_sync, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        canvas.pack(side="left", fill="both", expand=True)

        canvas.bind("<Configure>", lambda e, k=key: self._on_canvas_resize(k, e))
        canvas.bind("<Enter>", lambda e, k=key: setattr(self, "_hover_canvas_key", k))
        canvas.bind("<Leave>", lambda e: setattr(self, "_hover_canvas_key", None))

        # Traditional mouse wheel (real mice, Windows/Linux)
        def _wheel(e, k=key):
            if e.num == 4:
                units = -1
            elif e.num == 5:
                units = 1
            elif e.delta:
                # Windows sends ±120; macOS mice send small values. Use sign.
                units = -1 if e.delta > 0 else 1
            else:
                return
            self._scroll_sync(k, units)
            return "break"

        # Tk 9 on macOS: trackpad fires <TouchpadScroll>, not <MouseWheel>.
        def _touchpad(e, k=key):
            try:
                _dx, dy = self.tk.call("tk::PreciseScrollDeltas", e.delta)
            except Exception:
                return
            if dy:
                self._scroll_sync(k, -1 if dy > 0 else 1)
            return "break"

        for widget in (canvas, canvas_frame, panel):
            widget.bind("<MouseWheel>",     _wheel)
            widget.bind("<Button-4>",       _wheel)
            widget.bind("<Button-5>",       _wheel)
            widget.bind("<TouchpadScroll>", _touchpad)

        # Drag-select text in the page to add it as a keyword
        canvas.bind("<ButtonPress-1>",   lambda e, k=key: self._sel_begin(k, e))
        canvas.bind("<B1-Motion>",       lambda e, k=key: self._sel_drag(k, e))
        canvas.bind("<ButtonRelease-1>", lambda e, k=key: self._sel_end(k, e))

        self._prev_canvas[key] = canvas
        return panel

    def _toggle_panel(self, key: str):
        """Show or hide a preview panel in the right PanedWindow."""
        visible = self._prev_visible[key]
        btn     = self._show_btn[key]
        label   = "Original PDF" if key == "orig" else "Redacted PDF"

        if visible:
            self._preview_area.remove(self._prev_panel[key])
            self._prev_visible[key] = False
            if btn:
                btn.configure(text=f"Show {label}")
        else:
            if self._prev_panel[key] is None:
                self._prev_panel[key] = self._build_preview_panel(key, label)
            self._preview_area.add(self._prev_panel[key], minsize=150)
            self._prev_visible[key] = True
            if btn:
                btn.configure(text=f"Hide {label}")
            if self._prev_docs[key] is not None:
                self.after(100, lambda k=key: self._render_preview(k))
        # Re-balance the split so both panels share space equally
        self.after(120, self._balance_panes)

    def _balance_panes(self):
        """Place the sash so both panels share the preview area equally."""
        if not (self._prev_visible["orig"] and self._prev_visible["redact"]):
            return
        self._preview_area.update_idletasks()
        h = self._preview_area.winfo_height()
        if h > 40:
            try:
                self._preview_area.sash_place(0, 1, h // 2)
            except Exception:
                pass

    # ── pop-out preview window ────────────────────────────────────────────────

    def _toggle_popout(self):
        if self._popped_out:
            self._dock_previews()
        else:
            self._pop_out_previews()

    def _reset_preview_widgets(self):
        """Clear widget references after destroying the preview area."""
        for k in ("orig", "redact"):
            self._prev_panel[k]  = None
            self._prev_canvas[k] = None
            self._prev_label[k]  = None
            self._prev_btn[k]    = {}
            self._zoom_lbl[k]    = None
            self._goto_entry[k]  = None
            self._prev_visible[k] = False

    def _pop_out_previews(self):
        # Remember which panels were open so we can restore them
        was_visible = [k for k in ("orig", "redact") if self._prev_visible[k]]

        # Tear down the docked preview area (can't reparent in tkinter)
        self._preview_area.destroy()
        self._reset_preview_widgets()

        # New top-level window for the previews
        win = tk.Toplevel(self)
        win.title("PDF Previews")
        win.geometry("700x900")
        win.protocol("WM_DELETE_WINDOW", self._dock_previews)
        self._preview_win = win
        self._popped_out = True

        bar = ttk.Frame(win)
        bar.pack(fill="x")
        ttk.Button(bar, text="Dock back into main window",
                   command=self._dock_previews).pack(side="left", padx=6, pady=4)

        self._make_preview_area(win)
        self._preview_area.pack(fill="both", expand=True)

        # If nothing was open, show the Original panel so the window isn't blank
        for k in (was_visible or ["orig"]):
            self._toggle_panel(k)

        win.lift()
        win.focus_force()

        if self._popout_btn:
            self._popout_btn.configure(text="Dock")

    def _dock_previews(self):
        was_visible = [k for k in ("orig", "redact") if self._prev_visible[k]]

        self._preview_area.destroy()
        self._reset_preview_widgets()
        if self._preview_win is not None:
            self._preview_win.destroy()
            self._preview_win = None
        self._popped_out = False

        # Rebuild docked preview area inside the right-hand container
        self._make_preview_area(self._right)
        self._preview_area.pack(fill="both", expand=True)

        for k in was_visible:
            self._toggle_panel(k)

        if self._popout_btn:
            self._popout_btn.configure(text="Pop Out")

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
            self._set_text(self.kw_preview, "\n".join(kws))
            self._log(f"Loaded {len(kws)} keyword(s) from {self.keywords_path.name}", "info")
        except Exception as exc:
            self._show_error("Failed to read keywords file", exc)

    def _get_keywords(self) -> List[str]:
        """Read keywords from the editable text box (one per line, # = comment)."""
        raw = self.kw_preview.get("1.0", "end")
        out = []
        for line in raw.splitlines():
            term = line.strip()
            if term and not term.startswith("#"):
                out.append(term)
        return out

    def _add_keyword(self):
        term = self.kw_entry.get().strip()
        if not term:
            return
        existing = self._get_keywords()
        if term in existing:
            self._log(f"Keyword already present: {term!r}", "info")
        else:
            current = self.kw_preview.get("1.0", "end").rstrip("\n")
            new_text = (current + "\n" + term) if current else term
            self._set_text(self.kw_preview, new_text)
            self._log(f"Added keyword: {term!r}", "info")
        self.kw_entry.delete(0, "end")

    def _save_keywords(self):
        keywords = self._get_keywords()
        if not keywords:
            self._show_error("Nothing to save", "The keyword list is empty.")
            return
        path = self.keywords_path
        if path is None:
            chosen = filedialog.asksaveasfilename(
                title="Save keywords file",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            path.write_text("\n".join(keywords) + "\n", encoding="utf-8")
        except Exception as exc:
            self._show_error("Failed to save keywords file", exc)
            return
        self.keywords_path = path
        self.kw_label.configure(text=path.name, foreground="black")
        self._log(f"Saved {len(keywords)} keyword(s) to {path.name}", "info")

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
        first_new: Optional[Path] = None
        for p in paths:
            path = Path(p)
            if path not in self.pdf_paths:
                self.pdf_paths.append(path)
                self.pdf_listbox.insert("end", path.name)
                if first_new is None:
                    first_new = path
                added += 1
        if added:
            self._log(f"Added {added} PDF(s). Total queued: {len(self.pdf_paths)}", "info")
            # Auto-select the first new file and load its preview
            idx = self.pdf_paths.index(first_new)
            self.pdf_listbox.selection_clear(0, "end")
            self.pdf_listbox.selection_set(idx)
            self.pdf_listbox.see(idx)
            self._load_preview(first_new, "orig")

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

    def _on_listbox_select(self, _event=None):
        sel = self.pdf_listbox.curselection()
        if not sel:
            return
        path = self.pdf_paths[sel[0]]
        self._load_preview(path, "orig")
        # If this file has already been redacted, show its redacted output too
        redacted = self._redacted_map.get(path)
        if redacted and redacted.exists():
            self._load_preview(redacted, "redact")

    # ── redaction worker ──────────────────────────────────────────────────────

    def _start(self):
        keywords = self._get_keywords()
        if not keywords:
            self._show_error("No keywords",
                             "Please enter at least one keyword to redact.")
            return
        if not self.pdf_paths:
            self._show_error("No PDFs selected", "Please add at least one PDF file.")
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
        last_redacted_path: Optional[Path] = None

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
                    last_redacted_path = out_path
                    self._redacted_map[pdf_path] = out_path
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
        self._queue("done", (last_output_dir, last_redacted_path))

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
                    last_output_dir, last_redacted_path = data
                    self._on_done(last_output_dir, last_redacted_path)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    def _on_done(self, last_output_dir: Optional[Path], last_redacted_path: Optional[Path]):
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.file_label.configure(text="")
        self.page_bar["value"] = 0
        if last_output_dir and last_output_dir.is_dir():
            self._last_output_dir = last_output_dir
            self.open_out_btn.configure(state="normal")
        # Prefer the redacted output matching the currently-selected file so the
        # two previews stay aligned when multiple files were processed.
        sel = self.pdf_listbox.curselection()
        target = None
        if sel:
            target = self._redacted_map.get(self.pdf_paths[sel[0]])
        if target is None or not target.exists():
            target = last_redacted_path
        if target and target.exists():
            self._load_preview(target, "redact")

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

    # ── PDF preview ───────────────────────────────────────────────────────────

    def _load_preview(self, path: Path, key: str):
        """Open a PDF and display its first page in the given preview panel."""
        doc = self._prev_docs[key]
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        try:
            self._prev_docs[key] = fitz.open(path)
        except Exception as exc:
            self._log(f"Preview failed ({path.name}): {exc}", "warn")
            return
        self._prev_pages[key] = 0
        # Auto-show the panel if it isn't visible yet
        if not self._prev_visible[key]:
            self._toggle_panel(key)
        else:
            self._render_preview(key)

    def _render_preview(self, key: str):
        """Render the current page of the preview document onto the canvas."""
        doc = self._prev_docs[key]
        canvas = self._prev_canvas[key]
        if doc is None or canvas is None:
            return

        page_idx = self._prev_pages[key]
        total    = len(doc)

        # Wait until the canvas has real dimensions before rendering
        canvas.update_idletasks()
        w = canvas.winfo_width()
        if w < 10:
            w = 600

        page  = doc[page_idx]
        # Fit page to available width, then apply the user's zoom multiplier.
        fit_w = w - 4  # small margin
        fit_zoom = (fit_w / page.rect.width) if page.rect.width > 0 else 1.0
        fit_zoom = max(0.3, fit_zoom)
        zoom = fit_zoom * self._zoom_factor.get(key, 1.0)
        self._prev_zoom[key] = zoom
        mat   = fitz.Matrix(zoom, zoom)
        pix   = page.get_pixmap(matrix=mat)
        photo = tk.PhotoImage(data=base64.b64encode(pix.tobytes("png")).decode())

        self._prev_photos[key] = photo  # keep reference

        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo)
        # When zoomed in the page may be wider than the canvas — allow h-scroll.
        canvas.configure(scrollregion=(0, 0, max(w, pix.width), pix.height))

        zlbl = self._zoom_lbl.get(key)
        if zlbl:
            zlbl.configure(text=f"{round(self._zoom_factor.get(key, 1.0) * 100)}%")

        # Update nav label and button states
        lbl = self._prev_label[key]
        if lbl:
            lbl.configure(text=f"Page {page_idx + 1} / {total}")
        btns = self._prev_btn[key]
        if btns:
            btns["prev"].configure(state="normal" if page_idx > 0 else "disabled")
            btns["next"].configure(state="normal" if page_idx < total - 1 else "disabled")

    # ── drag-to-select keyword from a preview page ────────────────────────────

    def _sel_begin(self, key: str, event):
        canvas = self._prev_canvas[key]
        if canvas is None or self._prev_docs[key] is None:
            return
        x = canvas.canvasx(event.x)
        y = canvas.canvasy(event.y)
        self._sel_start[key] = (x, y)
        if self._sel_rect_id[key] is not None:
            canvas.delete(self._sel_rect_id[key])
        self._sel_rect_id[key] = canvas.create_rectangle(
            x, y, x, y, outline="#1565C0", width=2, dash=(3, 2))

    def _sel_drag(self, key: str, event):
        canvas = self._prev_canvas[key]
        start = self._sel_start[key]
        if canvas is None or start is None or self._sel_rect_id[key] is None:
            return
        x = canvas.canvasx(event.x)
        y = canvas.canvasy(event.y)
        canvas.coords(self._sel_rect_id[key], start[0], start[1], x, y)

    def _sel_end(self, key: str, event):
        canvas = self._prev_canvas[key]
        start  = self._sel_start[key]
        doc    = self._prev_docs[key]
        self._sel_start[key] = None
        if canvas is None or start is None or doc is None:
            return
        rid = self._sel_rect_id[key]
        if rid is not None:
            canvas.delete(rid)
            self._sel_rect_id[key] = None

        x1, y1 = start
        x2 = canvas.canvasx(event.x)
        y2 = canvas.canvasy(event.y)
        # Ignore tiny drags (treat as a click)
        if abs(x2 - x1) < 4 and abs(y2 - y1) < 4:
            return

        zoom = self._prev_zoom.get(key, 1.0) or 1.0
        # Map canvas (pixel) coords back to PDF point coords
        rect = fitz.Rect(min(x1, x2) / zoom, min(y1, y2) / zoom,
                         max(x1, x2) / zoom, max(y1, y2) / zoom)
        page = doc[self._prev_pages[key]]
        text = page.get_text("text", clip=rect).strip()
        # Collapse internal whitespace/newlines to single spaces
        text = " ".join(text.split())
        if not text:
            self._log("No selectable text in that region (scanned image?).", "warn")
            return
        self.kw_entry.delete(0, "end")
        self.kw_entry.insert(0, text)
        self._add_keyword()

    def _zoom(self, key: str, factor: Optional[float]):
        """Apply a zoom multiplier; factor=None resets to fit-width (100%)."""
        if self._prev_docs[key] is None:
            return
        if factor is None:
            self._zoom_factor[key] = 1.0
        else:
            new = self._zoom_factor.get(key, 1.0) * factor
            self._zoom_factor[key] = max(0.25, min(5.0, new))  # clamp 25%–500%
        self._render_preview(key)

    def _goto_page(self, key: str):
        doc = self._prev_docs[key]
        entry = self._goto_entry[key]
        if doc is None or entry is None:
            return
        raw = entry.get().strip()
        if not raw:
            return
        try:
            n = int(raw)
        except ValueError:
            self._log(f"Invalid page number: {raw!r}", "warn")
            return
        n = max(1, min(len(doc), n))
        self._prev_pages[key] = n - 1
        self._render_preview(key)
        self._sync_page(key)
        entry.delete(0, "end")

    def _prev_page(self, key: str):
        if self._prev_pages[key] > 0:
            self._prev_pages[key] -= 1
            self._render_preview(key)
            self._sync_page(key)

    def _next_page(self, key: str):
        doc = self._prev_docs[key]
        if doc and self._prev_pages[key] < len(doc) - 1:
            self._prev_pages[key] += 1
            self._render_preview(key)
            self._sync_page(key)

    def _sync_page(self, source_key: str):
        """Mirror the current page index to the other panel if it has a doc loaded."""
        other_key = "redact" if source_key == "orig" else "orig"
        if not self._prev_visible.get(other_key):
            return
        other_doc = self._prev_docs[other_key]
        if other_doc is None:
            return
        page_idx = self._prev_pages[source_key]
        page_idx = min(page_idx, len(other_doc) - 1)
        self._prev_pages[other_key] = page_idx
        self._render_preview(other_key)

    def _on_canvas_resize(self, key: str, _event=None):
        """Re-render when the canvas is resized so the page fills the new width."""
        if self._prev_docs[key] is not None:
            self._render_preview(key)

    def _on_root_wheel(self, event):
        """Fallback: scroll whichever PDF canvas the pointer is hovering over."""
        key = self._hover_canvas_key
        if key is None:
            return  # not over a PDF canvas — let the event reach its target normally
        if event.num == 4:
            units = -1
        elif event.num == 5:
            units = 1
        elif event.delta:
            units = -1 if event.delta > 0 else 1
        else:
            return
        self._scroll_sync(key, units)
        return "break"

    def _on_root_touchpad(self, event):
        """Fallback trackpad handler (Tk 9 macOS) for the hovered canvas."""
        key = self._hover_canvas_key
        if key is None:
            return
        try:
            _dx, dy = self.tk.call("tk::PreciseScrollDeltas", event.delta)
        except Exception:
            return
        if dy:
            self._scroll_sync(key, -1 if dy > 0 else 1)
        return "break"

    def _scroll_sync(self, source_key: str, units: int):
        """Scroll the source canvas; flip pages at the top/bottom edge.

        Both the scroll position and page changes are mirrored to the other
        panel so the two previews stay in lockstep.
        """
        src = self._prev_canvas[source_key]
        doc = self._prev_docs[source_key]
        if src is None or doc is None:
            return

        # After a page flip, swallow trackpad momentum briefly so the leftover
        # scroll events don't rush through the top of the new page.
        now = time.monotonic()
        if now < self._flip_cooldown_until:
            return

        before = src.yview()
        src.yview_scroll(units, "units")
        src.update_idletasks()
        after = src.yview()

        page_idx = self._prev_pages[source_key]
        last = len(doc) - 1

        # If the view didn't move, we're at a page edge — flip pages.
        moved = abs(after[0] - before[0]) > 1e-4
        if not moved:
            if units > 0 and page_idx < last:
                self._prev_pages[source_key] = page_idx + 1
                self._render_preview(source_key)
                src.yview_moveto(0.0)           # land at top of next page
                self._sync_page(source_key)
                self._mirror_scroll(source_key, 0.0)
                self._flip_cooldown_until = now + 0.6
                return
            if units < 0 and page_idx > 0:
                self._prev_pages[source_key] = page_idx - 1
                self._render_preview(source_key)
                src.yview_moveto(1.0)           # land at bottom of prev page
                self._sync_page(source_key)
                self._mirror_scroll(source_key, 1.0)
                self._flip_cooldown_until = now + 0.6
                return

        # Normal in-page scroll — mirror position to the other panel.
        self._mirror_scroll(source_key, src.yview()[0])

    def _mirror_scroll(self, source_key: str, fraction: float):
        other_key = "redact" if source_key == "orig" else "orig"
        other = self._prev_canvas.get(other_key)
        if other and self._prev_visible.get(other_key):
            other.yview_moveto(fraction)

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
        # Keep the editable keyword box writable; lock read-only widgets (log).
        if widget is not self.kw_preview:
            widget.configure(state="disabled")

    def _show_error(self, title: str, detail):
        msg = str(detail)
        self._log(f"ERROR -- {title}: {msg}", "err")
        messagebox.showerror(title, msg)


if __name__ == "__main__":
    app = PDFRedactorApp()
    app.mainloop()
