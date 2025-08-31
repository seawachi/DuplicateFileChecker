#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import queue
import threading
import hashlib
import traceback
import tempfile
import zipfile
import shutil
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------- Optional dependencies (only affect previews / recycle bin) ----------
try:
    from PIL import Image, ImageTk, ImageOps
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    import cv2  # needs numpy
    CV2_AVAILABLE = True
except Exception:
    CV2_AVAILABLE = False

try:
    from send2trash import send2trash
    SEND2TRASH_AVAILABLE = True
except Exception:
    SEND2TRASH_AVAILABLE = False


# --------------------------------- Models --------------------------------------

@dataclass
class FileInfo:
    path: str
    size: int
    mtime: float
    sha256: Optional[str] = None
    qhash: Optional[str] = None  # quick fingerprint

    def pretty_mtime(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.mtime))

    def pretty_size(self) -> str:
        if self.size <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = min(int(math.floor(math.log(self.size, 1024))), len(units) - 1)
        return f"{self.size / (1024 ** idx):.2f} {units[idx]}"


@dataclass
class DuplicateSet:
    digest: str
    files: List[FileInfo] = field(default_factory=list)

    @property
    def keeper(self) -> FileInfo:
        # Keep most recent
        return max(self.files, key=lambda f: f.mtime)

    def others(self) -> List[FileInfo]:
        k = self.keeper
        return [f for f in self.files if f.path != k.path]


# ------------------------------- Media helpers ---------------------------------

PREVIEWABLE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
PREVIEWABLE_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv"}

def ext_lower(path: str) -> str:
    return os.path.splitext(path)[1].lower()

def is_previewable_image(path: str) -> bool:
    return ext_lower(path) in PREVIEWABLE_IMAGE_EXTS

def is_previewable_video(path: str) -> bool:
    return ext_lower(path) in PREVIEWABLE_VIDEO_EXTS

def file_kind(path: str) -> str:
    e = ext_lower(path)
    if e in PREVIEWABLE_IMAGE_EXTS: return "img"
    if e in PREVIEWABLE_VIDEO_EXTS: return "vid"
    return "other"


# --------------------------------- Hashing -------------------------------------

def sha256_file(path: str, stop_event: threading.Event, chunk_size: int = 4 * 1024 * 1024) -> Optional[str]:
    """
    Compute SHA-256 in chunks; return None if canceled/unreadable.
    Larger chunk_size reduces Python overhead and usually speeds up SSDs.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                if stop_event.is_set():
                    return None
                b = f.read(chunk_size)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None


def quick_fingerprint(path: str, stop_event: threading.Event, head_bytes: int = 256 * 1024) -> Optional[str]:
    """
    Fast content fingerprint based on head+tail bytes.
    Safe optimization: ONLY partitions candidates; final equality uses full SHA-256.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if stop_event.is_set():
                return None
            if size <= head_bytes * 2:
                data = f.read()
                return hashlib.blake2b(data, digest_size=16).hexdigest()
            head = f.read(head_bytes)
            if stop_event.is_set():
                return None
            f.seek(-head_bytes, os.SEEK_END)
            tail = f.read(head_bytes)
        return hashlib.blake2b(head + tail, digest_size=16).hexdigest()
    except Exception:
        return None


def safe_os_walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        yield dirpath, dirnames, filenames


# --------------------------------- Worker --------------------------------------

class ScanWorker(threading.Thread):
    """
    Walks folders (and optional ZIPs), then:
      1) group by file size
      2) compute quick fingerprints in parallel to prune
      3) compute full SHA-256 in parallel to confirm TRUE duplicates
    Sends status/progress/results via out_q.
    """
    def __init__(self, root_dir: str, out_q: queue.Queue, stop_event: threading.Event,
                 scan_zip: bool, max_workers: int):
        super().__init__(daemon=True)
        self.root_dir = root_dir
        self.out_q = out_q
        self.stop_event = stop_event
        self.scan_zip = scan_zip
        self.max_workers = max(1, int(max_workers))
        self.temp_dir: Optional[str] = None

    # ----- messaging helpers -----
    def _status(self, msg: str): self.out_q.put(("status", msg))
    def _log(self, msg: str): self.out_q.put(("log", msg))

    # ----- zip expansion -----
    def _maybe_expand_zips(self) -> List[str]:
        roots = [self.root_dir]
        if not self.scan_zip:
            return roots
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="dup_scan_")
            self._log(f"Temp dir for ZIP extraction: {self.temp_dir}")
            for dirpath, _, filenames in safe_os_walk(self.root_dir):
                for name in filenames:
                    if name.lower().endswith(".zip"):
                        z_path = os.path.join(dirpath, name)
                        target = os.path.join(self.temp_dir, os.path.splitext(os.path.basename(z_path))[0])
                        try:
                            os.makedirs(target, exist_ok=True)
                            with zipfile.ZipFile(z_path, "r") as z:
                                z.extractall(target)
                            self._log(f"Extracted {z_path} → {target}")
                        except Exception as e:
                            self._log(f"ZIP extract failed: {z_path} — {e}")
            roots.append(self.temp_dir)
        except Exception as e:
            self._log(f"ZIP setup error: {e}")
        return roots

    # ----- main -----
    def run(self):
        try:
            self._status(f"Scanning: {self.root_dir}")
            roots = self._maybe_expand_zips()

            # 1) Collect candidates
            candidates: List[FileInfo] = []
            visited = 0
            denied = 0

            for root in roots:
                for dirpath, _, filenames in safe_os_walk(root):
                    for name in filenames:
                        if self.stop_event.is_set():
                            self._status("Scan canceled.")
                            return
                        visited += 1
                        if visited % 500 == 0:
                            self._status(f"Scanning… visited {visited:,} files")
                        path = os.path.join(dirpath, name)
                        try:
                            st = os.stat(path)
                            candidates.append(FileInfo(path=path, size=st.st_size, mtime=st.st_mtime))
                        except PermissionError:
                            denied += 1
                        except Exception:
                            pass

            self._log(f"Visited: {visited:,} | Readable: {len(candidates):,} | Permission-denied: {denied:,}")
            if not candidates:
                self.out_q.put(("done", []))
                self._status("No readable files found.")
                return

            # 2) Group by size
            self._status(f"Found {len(candidates):,} files. Grouping by size…")
            by_size: Dict[int, List[FileInfo]] = {}
            for fi in candidates:
                by_size.setdefault(fi.size, []).append(fi)

            size_groups = [g for g in by_size.values() if len(g) > 1]
            if not size_groups:
                self.out_q.put(("done", []))
                self._status("No duplicate-sized files found.")
                return

            # 3) Quick fingerprint (parallel): reduce full-hash work
            q_candidates = [fi for grp in size_groups for fi in grp]
            self._status(f"Quick fingerprinting {len(q_candidates):,} files using {self.max_workers} threads…")
            self.out_q.put(("progress_max", len(q_candidates)))

            hashed_q = 0
            report_every_q = max(10, len(q_candidates) // 100)

            def qtask(fi: FileInfo) -> Tuple[FileInfo, Optional[str]]:
                return fi, quick_fingerprint(fi.path, self.stop_event)

            by_qhash: Dict[Tuple[int, str], List[FileInfo]] = {}
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futmap = {ex.submit(qtask, fi): fi for fi in q_candidates}
                for fut in as_completed(futmap):
                    if self.stop_event.is_set():
                        self._status("Scan canceled.")
                        return
                    fi, qh = fut.result()
                    if qh:
                        fi.qhash = qh
                        by_qhash.setdefault((fi.size, qh), []).append(fi)
                    hashed_q += 1
                    if hashed_q % report_every_q == 0 or hashed_q == len(q_candidates):
                        self.out_q.put(("progress", hashed_q))
                        self._status(f"Quick pass… {hashed_q:,}/{len(q_candidates):,}")

            # 4) Full SHA-256 only where quick hash collides
            full_list: List[FileInfo] = []
            for (_size, _qh), files in by_qhash.items():
                if len(files) > 1:
                    full_list.extend(files)

            if not full_list:
                self.out_q.put(("done", []))
                self._status("No duplicates after quick pass.")
                return

            self._status(f"Confirming duplicates (SHA-256) on {len(full_list):,} files…")
            self.out_q.put(("progress_max", len(full_list)))

            by_sha: Dict[str, List[FileInfo]] = {}
            hashed = 0
            report_every = max(10, len(full_list) // 100)

            def stag(fi: FileInfo) -> Tuple[FileInfo, Optional[str]]:
                return fi, sha256_file(fi.path, self.stop_event)

            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futmap = {ex.submit(stag, fi): fi for fi in full_list}
                for fut in as_completed(futmap):
                    if self.stop_event.is_set():
                        self._status("Scan canceled.")
                        return
                    fi, digest = fut.result()
                    if digest:
                        fi.sha256 = digest
                        by_sha.setdefault(digest, []).append(fi)
                    hashed += 1
                    if hashed % report_every == 0 or hashed == len(full_list):
                        self.out_q.put(("progress", hashed))
                        self._status(f"Hashing… {hashed:,}/{len(full_list):,}")

            # 5) Build duplicate sets (true duplicates only)
            dup_sets: List[DuplicateSet] = []
            for digest, files in by_sha.items():
                if len(files) > 1:
                    files.sort(key=lambda f: f.mtime, reverse=True)
                    dup_sets.append(DuplicateSet(digest=digest, files=files))

            dup_sets.sort(key=lambda ds: (len(ds.files), ds.keeper.mtime if ds.files else 0), reverse=True)

            if self.temp_dir:
                self.out_q.put(("tempdir", self.temp_dir))
            self.out_q.put(("done", dup_sets))
            self._status(f"Completed. Duplicate sets: {len(dup_sets)}")

        except Exception as e:
            self.out_q.put(("error", f"{e}\n{traceback.format_exc()}"))


# ----------------------------------- UI ---------------------------------------

class DuplicateMediaFocusedApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Duplicate File Cleaner — Fast & Safe (SHA-256, Multithreaded)")
        self.root.geometry("1400x760")
        self.root.minsize(980, 640)

        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Small.TLabel", font=("Segoe UI", 9))
        style.configure("Path.TLabel", font=("Menlo", 9) if os.name == "posix" else ("Consolas", 9))
        style.configure("Danger.TButton", foreground="#b00020")
        style.configure("Keeper.TLabel", foreground="#0b6e0b", font=("Segoe UI", 9, "bold"))

        # state
        self.selected_dir: Optional[str] = None
        self.out_q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[ScanWorker] = None

        # aggressive default threads (IO-bound): up to 32 or 4× CPUs
        default_threads = min(32, (os.cpu_count() or 4) * 4)
        self.threads_var = tk.IntVar(value=default_threads)
        self.auto_scan_var = tk.BooleanVar(value=True)
        self.media_only_var = tk.BooleanVar(value=False)
        self.scan_zip_var = tk.BooleanVar(value=False)

        self.dup_sets: List[DuplicateSet] = []
        self.view_sets: List[DuplicateSet] = []
        self.current_set_index: Optional[int] = None
        self.thumbnail_refs: List[tk.PhotoImage] = []
        self.check_vars: Dict[str, tk.BooleanVar] = {}
        self.temp_dirs: List[str] = []

        # references for new quick-prune bar buttons
        self.btn_bulk_del_all = None
        self.btn_bulk_del_set = None

        self._build_topbar()
        self._build_quick_prune_bar()  # <-- NEW section under the Select Folder bar
        self._build_body()
        self._build_statusbar()

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ---------- layout ----------
    def _build_topbar(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        left = ttk.Frame(top)
        left.pack(side=tk.LEFT)
        ttk.Button(left, text="Select Folder…", command=self.on_browse).pack(side=tk.LEFT)
        self.dir_label = ttk.Label(left, text="No folder selected", style="Small.TLabel")
        self.dir_label.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_scan = ttk.Button(left, text="Scan", command=self.on_scan, state=tk.DISABLED)
        self.btn_scan.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_stop = ttk.Button(left, text="Stop", command=self.on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(6, 0))

        right = ttk.Frame(top)
        right.pack(side=tk.RIGHT)

        ttk.Label(right, text="Threads:", style="Small.TLabel").pack(side=tk.RIGHT, padx=(8, 4))
        self.threads_spin = ttk.Spinbox(right, from_=1, to=128, width=4, textvariable=self.threads_var, justify="center")
        self.threads_spin.pack(side=tk.RIGHT, padx=(0, 12))

        ttk.Checkbutton(right, text="Auto-scan on select", variable=self.auto_scan_var).pack(side=tk.RIGHT, padx=(8, 8))
        ttk.Checkbutton(right, text="Scan inside ZIPs", variable=self.scan_zip_var).pack(side=tk.RIGHT, padx=(0, 12))
        ttk.Checkbutton(right, text="Show only image/video sets", variable=self.media_only_var, command=self._rebuild_tree).pack(side=tk.RIGHT)

        counts = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        counts.pack(fill=tk.X)
        self.count_label = ttk.Label(counts, text="Sets: 0 • Media sets: 0 • Hidden by filter: 0", style="Small.TLabel")
        self.count_label.pack(anchor="w")
        self.banner = ttk.Label(self.root, text="", foreground="#b26b00", style="Small.TLabel")
        self.banner.pack(fill=tk.X, padx=12)

    def _build_quick_prune_bar(self):
        """
        A pinned bar directly under the Select Folder area with two big buttons:
        - Delete Selected Set (keep latest)
        - Delete All Sets (keep latest)
        """
        bar = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        bar.pack(fill=tk.X)

        ttk.Label(bar, text="Quick Prune:", style="Small.TLabel").pack(side=tk.LEFT)

        # Right-aligned button cluster
        cluster = ttk.Frame(bar)
        cluster.pack(side=tk.RIGHT)

        self.btn_bulk_del_all = ttk.Button(
            cluster, text="Delete All Sets (keep latest)", style="Danger.TButton",
            command=self.on_delete_all_sets_keep_latest, state=tk.DISABLED
        )
        self.btn_bulk_del_all.pack(side=tk.RIGHT, padx=(6, 0))

        self.btn_bulk_del_set = ttk.Button(
            cluster, text="Delete Selected Set (keep latest)", style="Danger.TButton",
            command=self.on_delete_this_set_keep_latest, state=tk.DISABLED
        )
        self.btn_bulk_del_set.pack(side=tk.RIGHT)

    def _build_body(self):
        body = ttk.Frame(self.root, padding=(12, 0, 12, 0))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # ---------------- Left list (duplicate sets) ----------------
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
        ttk.Label(left, text="Duplicate Sets", style="Header.TLabel").pack(anchor="w", pady=(6, 6))

        columns = ("idx","count","types","digest","keeper")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=22)
        for cid, title, width, anchor in [
            ("idx","#",40,"center"),
            ("count","Files",60,"e"),
            ("types","Types",90,"center"),
            ("digest","SHA-256 (short)",220,"w"),
            ("keeper","Keeper (most recent)",320,"w"),
        ]:
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=width, anchor=anchor)

        yscroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH)
        yscroll.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_set)

        # ---------------- Right panel ----------------
        right = ttk.Frame(body)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Header row with title (left) + actions (right) pinned above the scroll area
        header = ttk.Frame(right)
        header.pack(fill=tk.X, pady=(6, 6))

        ttk.Label(header, text="Preview & Delete (media-focused)", style="Header.TLabel")\
            .pack(side=tk.LEFT)

        header_actions = ttk.Frame(header)
        header_actions.pack(side=tk.RIGHT)

        # Recycle bin toggle
        self.recycle_var = tk.BooleanVar(value=SEND2TRASH_AVAILABLE)
        rec = ttk.Checkbutton(header_actions, text="Use Recycle Bin", variable=self.recycle_var)
        if not SEND2TRASH_AVAILABLE:
            rec.state(["disabled"])
        rec.pack(side=tk.LEFT, padx=(0, 10))

        # Action buttons from right to left (dangerous actions on the far right)
        ttk.Button(header_actions, text="Delete Selected", style="Danger.TButton",
                   command=self.on_delete_selected).pack(side=tk.RIGHT, padx=(3,0))
        ttk.Button(header_actions, text="Select All Except Latest",
                   command=self.on_select_all_but_keeper).pack(side=tk.RIGHT, padx=(3,0))
        ttk.Button(header_actions, text="Select None",
                   command=self.on_select_none).pack(side=tk.RIGHT)
        ttk.Button(header_actions, text="Select All",
                   command=self.on_select_all).pack(side=tk.RIGHT)

        ttk.Separator(right, orient="horizontal").pack(fill=tk.X, pady=(0, 4))

        # Scrollable preview area (only this part scrolls)
        vp = ttk.Frame(right)
        vp.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(vp, borderwidth=0, highlightthickness=0)
        scroll_y = ttk.Scrollbar(vp, orient="vertical", command=self.canvas.yview)
        inner = ttk.Frame(self.canvas)
        inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0,0), window=inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scroll_y.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel support
        def _wheel(e): self.canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        self.canvas.bind_all("<MouseWheel>", _wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))  # Linux
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))   # Linux

        self.detail_container = inner

        # Log area
        log_frame = ttk.Frame(right)
        log_frame.pack(fill=tk.X, pady=(4, 8))
        ttk.Label(log_frame, text="Log:", style="Small.TLabel").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=6, wrap="word")
        self.log_text.configure(state="disabled")
        self.log_text.pack(fill=tk.BOTH, expand=False)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(12,6,12,10))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = ttk.Label(bar, text="Ready.", style="Small.TLabel")
        self.progress = ttk.Progressbar(bar, mode="determinate", length=260)
        self.status_label.pack(side=tk.LEFT)
        self.progress.pack(side=tk.RIGHT)

    # ---------- events ----------
    def on_browse(self):
        path = filedialog.askdirectory(title="Select a folder to scan for duplicates")
        if not path: return
        self.selected_dir = path
        self.dir_label.config(text=path)
        self.btn_scan.config(state=tk.NORMAL)
        self._log_ui(f"Selected: {path}")
        if self.auto_scan_var.get():
            self.on_scan()

    def on_scan(self):
        if not self.selected_dir:
            messagebox.showinfo("Select folder", "Please select a folder first.")
            return
        self._clear_results()
        self.progress.configure(value=0, maximum=100)
        self.status_label.config(text="Starting scan…")
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.stop_event.clear()
        self._set_quick_prune_enabled(False, False)
        threads = max(1, int(self.threads_var.get()))
        self._log_ui(f"Scan started in: {self.selected_dir} | ZIPs: {self.scan_zip_var.get()} | Threads: {threads}")
        self.worker = ScanWorker(self.selected_dir, self.out_q, self.stop_event, self.scan_zip_var.get(), threads)
        self.worker.start()

    def on_stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_label.config(text="Stopping…")
            self._log_ui("Stop requested.")
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_scan.config(state=tk.NORMAL)

    def on_select_set(self, _evt):
        sel = self.tree.selection()
        if not sel:
            self.current_set_index = None
            self._set_quick_prune_enabled(bool(self.dup_sets), False)
            return
        try:
            idx = int(self.tree.item(sel[0], "values")[0]) - 1
        except Exception:
            self._set_quick_prune_enabled(bool(self.dup_sets), False)
            return
        if 0 <= idx < len(self.view_sets):
            self.current_set_index = idx
            self._render_detail(self.view_sets[idx])
            self._set_quick_prune_enabled(True, True)

    def on_select_all(self):
        for v in self.check_vars.values(): v.set(True)

    def on_select_none(self):
        for v in self.check_vars.values(): v.set(False)

    def on_select_all_but_keeper(self):
        ds = self._current_set()
        if not ds: return
        keep = ds.keeper.path
        for p, v in self.check_vars.items(): v.set(p != keep)

    def on_delete_selected(self):
        ds = self._current_set()
        if not ds: return
        to_delete = [p for p, v in self.check_vars.items() if v.get()]
        if not to_delete:
            messagebox.showinfo("Nothing selected", "Select one or more files to delete.")
            return
        self._confirm_and_delete(to_delete, ds)

    # ---- quick actions (keep latest always) ----
    def on_delete_this_set_keep_latest(self):
        ds = self._current_set()
        if not ds:
            messagebox.showinfo("No set selected", "Select a duplicate set on the left first.")
            return
        keeper = ds.keeper.path
        to_delete = [f.path for f in ds.files if f.path != keeper]
        if not to_delete:
            messagebox.showinfo("Nothing to delete", "This set only has one file.")
            return
        self._confirm_and_delete(to_delete, ds)

    def on_delete_all_sets_keep_latest(self):
        if not self.dup_sets:
            messagebox.showinfo("Nothing to delete", "No duplicate sets loaded.")
            return
        plan = []
        for ds in list(self.dup_sets):
            keeper = ds.keeper.path
            plan.extend([f.path for f in ds.files if f.path != keeper])
        if not plan:
            messagebox.showinfo("Nothing to delete", "No deletable files found.")
            return
        msg = f"You're about to delete {len(plan)} file(s) across {len(self.dup_sets)} set(s).\n\n"
        msg += "The most recent file in each set will be kept.\n\n"
        msg += "This cannot be undone" + (" (Recycle Bin)" if self.recycle_var.get() and SEND2TRASH_AVAILABLE else "") + ". Continue?"
        if not messagebox.askyesno("Confirm deletion", msg, icon="warning"):
            return
        failures, deleted = [], 0
        for path in plan:
            try:
                if self.recycle_var.get() and SEND2TRASH_AVAILABLE: send2trash(path)
                else: os.remove(path)
                deleted += 1
                for ds in list(self.dup_sets):
                    ds.files = [f for f in ds.files if f.path != path]
            except Exception as e:
                failures.append((path, str(e)))
        self.dup_sets = [ds for ds in self.dup_sets if len(ds.files) > 1]
        self.current_set_index = None
        self._rebuild_tree()
        self._clear_detail()
        self._set_quick_prune_enabled(bool(self.dup_sets), False)
        self.status_label.config(text=f"Deleted {deleted} file(s)." + (f" {len(failures)} failed." if failures else ""))
        if failures:
            messagebox.showwarning("Some deletions failed", "\n".join(f"• {p} — {err}" for p, err in failures))

    # ---- internal delete helper ----
    def _confirm_and_delete(self, paths: List[str], ds: DuplicateSet):
        msg = "You're about to delete:\n\n" + "\n".join(f"• {p}" for p in paths)
        msg += "\n\nThis cannot be undone" + (" (Recycle Bin)" if self.recycle_var.get() and SEND2TRASH_AVAILABLE else "") + ". Continue?"
        if not messagebox.askyesno("Confirm deletion", msg, icon="warning"):
            return
        failures, deleted = [], 0
        for p in paths:
            try:
                if self.recycle_var.get() and SEND2TRASH_AVAILABLE: send2trash(p)
                else: os.remove(p)
                deleted += 1
                ds.files = [f for f in ds.files if f.path != p]
            except Exception as e:
                failures.append((p, str(e)))
        if len(ds.files) <= 1:
            try: self.dup_sets.remove(ds)
            except Exception: pass
            self.current_set_index = None
            self._rebuild_tree()
            self._clear_detail()
            self._set_quick_prune_enabled(bool(self.dup_sets), False)
        else:
            self._render_detail(ds)
            self._rebuild_tree()
            self._set_quick_prune_enabled(True, True)
        self.status_label.config(text=f"Deleted {deleted} file(s)." + (f" {len(failures)} failed." if failures else ""))
        if failures:
            messagebox.showwarning("Some deletions failed", "\n".join(f"• {p} — {err}" for p, err in failures))

    def on_quit(self):
        for d in getattr(self, "temp_dirs", []):
            try: shutil.rmtree(d, ignore_errors=True)
            except Exception: pass
        self.root.destroy()

    # ---------- helpers ----------
    def _set_quick_prune_enabled(self, any_sets: bool, have_selection: bool):
        """Enable/disable the new quick-prune buttons."""
        if self.btn_bulk_del_all:
            (self.btn_bulk_del_all.state(["!disabled"]) if any_sets else self.btn_bulk_del_all.state(["disabled"]))
        if self.btn_bulk_del_set:
            (self.btn_bulk_del_set.state(["!disabled"]) if have_selection else self.btn_bulk_del_set.state(["disabled"]))

    def _current_set(self) -> Optional[DuplicateSet]:
        if self.current_set_index is None: return None
        return self.view_sets[self.current_set_index] if 0 <= self.current_set_index < len(self.view_sets) else None

    def _clear_results(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        self.dup_sets, self.view_sets = [], []
        self.current_set_index = None
        self._clear_detail()
        self._update_counts(0, 0, 0)
        self.banner.config(text="")
        self._set_quick_prune_enabled(False, False)

    def _clear_detail(self):
        for w in self.detail_container.winfo_children(): w.destroy()
        self.thumbnail_refs.clear()
        self.check_vars.clear()

    def _short_digest(self, d: str) -> str:
        return d[:12] + "…" if d and len(d) > 12 else (d or "")

    def _set_types_label(self, ds: DuplicateSet) -> str:
        kinds = {file_kind(f.path) for f in ds.files}
        order = ["img", "vid", "other"]
        label_map = {"img":"IMG","vid":"VID","other":"OTH"}
        return ",".join(label_map[k] for k in order if k in kinds)

    def _update_counts(self, total_sets: int, media_sets: int, hidden: int):
        self.count_label.config(text=f"Sets: {total_sets} • Media sets: {media_sets} • Hidden by filter: {hidden}")
        if self.media_only_var.get() and total_sets > 0 and hidden == total_sets:
            self.banner.config(text="No media duplicates found. Turn OFF the filter to see non-media duplicates.")
        else:
            self.banner.config(text="")

    def _rebuild_tree(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        media_sets = [ds for ds in self.dup_sets if any(file_kind(f.path) in ("img","vid") for f in ds.files)]
        total_sets = len(self.dup_sets)
        self.view_sets = media_sets if self.media_only_var.get() else list(self.dup_sets)
        hidden = total_sets - len(self.view_sets) if self.media_only_var.get() else 0
        self._update_counts(total_sets, len(media_sets), hidden)

        for i, ds in enumerate(self.view_sets, start=1):
            self.tree.insert("", "end", values=(i, len(ds.files), self._set_types_label(ds), self._short_digest(ds.digest), ds.keeper.path))

        if self.view_sets:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first); self.tree.focus(first); self.tree.see(first)
            self.current_set_index = 0
            self._render_detail(self.view_sets[0])
            self._set_quick_prune_enabled(True, True)
        else:
            self.current_set_index = None
            self._clear_detail()
            self._set_quick_prune_enabled(False, False)

    def _render_detail(self, ds: DuplicateSet):
        self._clear_detail()
        header = ttk.Frame(self.detail_container); header.pack(fill=tk.X, pady=(2,6))
        ttk.Label(header, text=f"Set digest: {ds.digest}", style="Small.TLabel").pack(anchor="w")

        for fi in ds.files:
            row = ttk.Frame(self.detail_container, padding=(4,6)); row.pack(fill=tk.X, expand=True)
            thumb = self._make_thumbnail(fi.path, (180,180))
            if thumb is None:
                ph = tk.Canvas(row, width=180, height=180, bg="#efefef", highlightthickness=1, highlightbackground="#ccc")
                kind = file_kind(fi.path)
                txt = "No preview" if kind == "other" else ("(image preview unavailable)" if kind=="img" else "(video preview unavailable)")
                ph.create_text(90, 90, text=txt, fill="#666", width=160, justify="center"); ph.pack(side=tk.LEFT, padx=(0,10))
            else:
                lbl = ttk.Label(row, image=thumb); lbl.pack(side=tk.LEFT, padx=(0,10)); self.thumbnail_refs.append(thumb)

            right = ttk.Frame(row); right.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Label(right, text=fi.path, style="Path.TLabel", wraplength=760, justify="left").pack(anchor="w")
            meta = ttk.Frame(right); meta.pack(anchor="w", pady=(4,0))
            ttk.Label(meta, text=f"Modified: {fi.pretty_mtime()} • Size: {fi.pretty_size()} • Type: {file_kind(fi.path).upper()}",
                      style="Small.TLabel").pack(side=tk.LEFT)

            cvar = tk.BooleanVar(value=(fi.path != ds.keeper.path))
            chk = ttk.Checkbutton(right, text="Mark for deletion", variable=cvar)
            self.check_vars[fi.path] = cvar
            if fi.path == ds.keeper.path:
                chk.state(["disabled"])
                ttk.Label(right, text="(keeper — most recent)", style="Keeper.TLabel").pack(anchor="w", pady=(2,0))
            chk.pack(anchor="w", pady=(6,0))

        ttk.Frame(self.detail_container, height=6).pack()

    def _make_thumbnail(self, path: str, size: Tuple[int,int]) -> Optional[tk.PhotoImage]:
        try:
            if is_previewable_image(path):
                if not PIL_AVAILABLE:
                    if path.lower().endswith((".png",".gif")):  # Tk fallback
                        return tk.PhotoImage(file=path)
                    return None
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im); im.thumbnail(size, Image.LANCZOS)
                    if im.mode not in ("RGB","RGBA"): im = im.convert("RGB")
                    return ImageTk.PhotoImage(im)
            if is_previewable_video(path):
                if not (CV2_AVAILABLE and PIL_AVAILABLE): return None
                cap = cv2.VideoCapture(path); ok, frame = cap.read(); cap.release()
                if not ok or frame is None: return None
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                from PIL import Image as _Img
                im = _Img.fromarray(frame); im.thumbnail(size, Image.LANCZOS)
                return ImageTk.PhotoImage(im)
            return None
        except Exception:
            return None

    def _log_ui(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                kind, *payload = self.out_q.get_nowait()
                if kind == "status":
                    self.status_label.config(text=payload[0]); self._log_ui(payload[0])
                elif kind == "progress_max":
                    self.progress.configure(value=0, maximum=payload[0])
                elif kind == "progress":
                    self.progress.configure(value=payload[0])
                elif kind == "done":
                    self.dup_sets = payload[0]
                    self.btn_stop.config(state=tk.DISABLED); self.btn_scan.config(state=tk.NORMAL)
                    self._rebuild_tree()
                elif kind == "error":
                    msg = payload[0]
                    self.btn_stop.config(state=tk.DISABLED); self.btn_scan.config(state=tk.NORMAL)
                    self._log_ui("ERROR:\n" + msg); messagebox.showerror("Error during scan", msg)
                elif kind == "log":
                    self._log_ui(payload[0])
                elif kind == "tempdir":
                    td = payload[0]
                    if td and os.path.isdir(td): self.temp_dirs.append(td); self._log_ui(f"Registered temp dir: {td}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def run(self):
        self.root.mainloop()


# --------------------------------- main ---------------------------------------

if __name__ == "__main__":
    app = DuplicateMediaFocusedApp()
    app.run()
