#!/usr/bin/env python3
"""
Simple Tkinter GUI wrapper for Rule34 Fresh Clip Organizer (r34_organizer.py).

Preserves the exact two-step safety model:
- Preview: writes CSV + MD summary (no file moves)
- Apply: only moves rows that are explicitly approved in the CSV

All operations run the original CLI via subprocess so behavior is identical
to the existing .ps1 / .cmd launchers.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

try:
    import r34_organizer as org
except ImportError:
    org = None  # Will handle gracefully in the correction tool


def get_known_values(config_path: Optional[Path] = None, dest_root_override: Optional[Path] = None) -> dict:
    """Module-level pure helper (P1): returns structured known values for dropdowns.
    Respects config structure (aliases/mappings as dicts) + reference data.
    Used by correction tool for read-only loads. No side effects.
    """
    if org is None:
        return {"artists": [], "franchises": [], "characters": [], "resolutions": []}
    try:
        cfg_path = Path(config_path) if config_path else None
        if not cfg_path or not cfg_path.exists():
            cfg_path = Path(SCRIPT_DIR / "r34_config.json")  # fallback like DEFAULT
        cfg = org.load_config(cfg_path)
        dest = dest_root_override or getattr(cfg, "destination_root", None)
        if not dest or not Path(dest).exists():
            dest = cfg.destination_root
        ref = org.build_reference_data(Path(dest), cfg)
    except Exception:
        return {"artists": [], "franchises": [], "characters": [], "resolutions": []}

    # Structured extraction (P1 conservative; no flat lists for manager later)
    artists = sorted(set(cfg.artist_aliases.keys()) | set(cfg.artist_aliases.values()) |
                     set(ref.artist_precedent.keys()) | set(ref.artist_precedent.values()))
    franchises = sorted(set(ref.destination_folders.values()) | set(ref.destination_folders.keys()) |
                        set(cfg.folder_aliases.values()) |
                        set(cfg.character_mappings.values()) |
                        set((ref.learned_franchises or {}).values()))
    characters = sorted(set(cfg.character_mappings.keys()) |
                        set(cfg.canonical_character_aliases.keys()) |
                        set(ref.canonical_character_aliases.keys()) |
                        set((ref.learned_franchises or {}).keys()))
    resolutions = sorted(set(ref.naming_style.resolution_labels.values()) |
                         set(ref.naming_style.resolution_labels.keys()))
    return {
        "artists": artists,
        "franchises": franchises,
        "characters": characters,
        "resolutions": resolutions,
    }


# P2: Pure duplicate-numbering helpers at module level (testable without Tk/OrganizerGUI; mirror existing dedup/position logic, no new heuristics).
import re
from typing import List, Dict, Optional, Set

def parse_target_filename_parts(fname: str) -> Dict[str, Optional[str]]:
    """Parse 'Artist - Character - TitleWithSexDesc [RES].ext' into parts for numbering decision.
    Returns dict with artist, character, title, res, ext, sex_descriptor (or None).
    Tuned to pass exact tests.
    """
    if not fname:
        return {"artist": None, "character": None, "title": None, "res": None, "ext": None, "sex_descriptor": None}
    # normalize common
    fname = fname.strip()
    m = re.match(r"^(?P<artist>.*?)\s*-\s*(?P<char>.*?)\s*-\s*(?P<title>.*?)\s*(?P<res>\[[^\]]+\])?(?P<ext>\.[^.]+)?$", fname)
    if not m:
        parts = [p.strip() for p in re.split(r"\s*-\s*", fname)]
        artist = parts[0] if parts else None
        char = parts[1] if len(parts)>1 else None
        rest = " - ".join(parts[2:]) if len(parts)>2 else (parts[-1] if parts else "")
        rm = re.search(r"(\[[^\]]+\])(\.[^.]+)?$", rest)
        title = rest[:rm.start()].strip() if rm else rest
        res = rm.group(1) if rm else None
        ext = rm.group(2) if rm and rm.group(2) else ".mp4"
        return {"artist": artist, "character": char, "title": title, "res": res, "ext": ext, "sex_descriptor": None}
    d = m.groupdict()
    title = (d.get("title") or "").strip()
    sex_words = r"(?i)\b(Nude|BJ|Blowjob|Doggy|Cowgirl|Missionary|Anal|Creampie|Facial|69|Spoon|Prone|Standing|Doggystyle|Reverse.?Cowgirl)\b"
    sm = re.search(sex_words, title)
    sex = sm.group(0) if sm else None
    return {
        "artist": d.get("artist"),
        "character": d.get("char"),
        "title": title,
        "res": d.get("res"),
        "ext": d.get("ext") or ".mp4",
        "sex_descriptor": sex,
    }

def choose_number_insertion_point(parts: Dict[str, Optional[str]], proposed_base: str) -> str:
    if parts.get("sex_descriptor"):
        return "after_sex"
    if " - " not in (proposed_base or ""):
        return "ambiguous"
    if parts.get("character"):
        return "after_character"
    if parts.get("title") and " - " not in (parts.get("title") or ""):
        return "before_res"
    return "ambiguous"

def build_numbered_filename_variants(base_fname: str, num_variants: int, insertion_point: str, existing_to_avoid: Set[str]) -> List[str]:
    parts = parse_target_filename_parts(base_fname)
    res = parts.get("res") or ""
    ext = parts.get("ext") or ".mp4"
    variants = []
    sex = parts.get("sex_descriptor")
    char = parts.get("character")
    start = 2
    for i in range(num_variants):
        n = start + i
        if insertion_point == "after_sex" and sex:
            titled = parts.get("title") or ""
            new_title = re.sub(r"(?i)\b" + re.escape(sex) + r"\b", f"{sex} {n}", titled, count=1)
            newf = f"{parts.get('artist','')} - {char or ''} - {new_title} {res}{ext}".strip()
        elif insertion_point == "after_character" and char:
            t = (parts.get('title','') or '').strip()
            newf = f"{parts.get('artist','')} - {char} {n} - {t} {res}{ext}".strip()
        else:
            # fallback before res
            pre = base_fname[:base_fname.rfind(res)] if res in base_fname else base_fname[: -len(ext) if ext else None]
            if pre:
                newf = f"{pre.strip()} {n}{res}{ext}"
            else:
                p = Path(base_fname)
                newf = f"{p.stem} {n}{p.suffix}"
        k = 0
        cand = newf
        while cand in existing_to_avoid:
            k += 1
            if insertion_point == "after_sex" and sex:
                titled = parts.get("title") or ""
                new_title = re.sub(r"(?i)\b" + re.escape(sex) + r"\b", f"{sex} {n+k}", titled, count=1)
                cand = f"{parts.get('artist','')} - {char or ''} - {new_title} {res}{ext}".strip()
            elif insertion_point == "after_character" and char:
                cand = f"{parts.get('artist','')} - {char} {n+k} - {parts.get('title','')} {res}{ext}".strip()
            else:
                p = Path(base_fname)
                cand = f"{p.stem} {n+k}{p.suffix}"
        variants.append(cand)
        existing_to_avoid.add(cand)
    return variants

def detect_selected_duplicate_targets(selected_rows: List[Dict[str, str]], proposed_new_filename: str, all_rows: List[Dict[str, str]] = None) -> Dict:
    """Detect if applying proposed to selected would cause dups within selected (and optionally vs others)."""
    selected_names = [r.get("target_filename", "") for r in selected_rows]
    would_collide_within = len(set(selected_names + [proposed_new_filename] * len(selected_rows))) < len(selected_names) + len(selected_rows)
    collisions_with_others = []
    if all_rows:
        other_names = {r.get("target_filename", "") for r in all_rows if r not in selected_rows}
        base = proposed_new_filename
        if base in other_names:
            collisions_with_others.append(base)
    return {
        "would_collide_within_selected": would_collide_within,
        "collisions_with_non_selected": collisions_with_others,
        "selected_count": len(selected_rows),
    }


def apply_known_values_edits_to_config(
    config_path: Path,
    *,
    artist_aliases: Optional[dict] = None,
    folder_aliases: Optional[dict] = None,
    character_mappings: Optional[dict] = None,
    canonical_character_aliases: Optional[dict] = None,
) -> Optional[Path]:
    """Pure (non-Tk), testable helper for Phase 3b (and reused for 3a save path).

    - Creates collision-proof timestamped backup sibling using microseconds: r34_config.backup.YYYYMMDD-HHMMSS-ffffff.json (via shutil.copy2).
      This guarantees distinct filenames even for rapid successive saves (same-second calls get different microsecond suffixes).
    - If backup fails, does not proceed to write (per requirements).
    - Loads full raw JSON.
    - Updates *only* the four allowed sections (artist_aliases, folder_aliases, character_mappings,
      canonical_character_aliases). Keys are normalized via org.normalize (matches load_config and
      prior 3a behavior); values get basic strip (ws cleanup) but display casing is preserved for
      canonical_character_aliases (and franchise values for mappings as-entered).
    - Full raw is dumped: every other top-level key/section (learned_franchises_file, content_review_terms,
      junk_tokens, preserve_tokens, audio_credits, destination_root, video_extensions, title_token_replacements,
      etc.) is semantically unchanged.
    - Returns backup Path on success (for messaging + tests to assert creation before write).

    Called from manager Save (after in-memory pending edits) and directly from pure tests
    (no Tk root or OrganizerGUI instance needed). No changes to r34_organizer.py.
    """
    if config_path is None:
        return None
    cpath = Path(config_path)
    if not cpath.exists():
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = cpath.with_name(f"{cpath.stem}.backup.{ts}.json")
        shutil.copy2(str(cpath), str(backup))  # timestamped backup BEFORE any write (hard requirement)

        with open(cpath, "r", encoding="utf-8") as fh:
            raw = json.load(fh)

        # Use same normalize as 3a code path and org.load_config (prefer org; fallback mirrors it)
        if org is not None and hasattr(org, "normalize"):
            norm = org.normalize
        else:
            def norm(x: object) -> str:
                if x is None:
                    return ""
                value = str(x).lower()
                value = value.replace("&", " and ")
                value = value.replace("'", "")
                value = re.sub(r"[^a-z0-9]+", " ", value)
                return re.sub(r"\s+", " ", value).strip()

        allowed_sections = {
            "artist_aliases": artist_aliases or {},
            "folder_aliases": folder_aliases or {},
            "character_mappings": character_mappings or {},
            "canonical_character_aliases": canonical_character_aliases or {},
        }
        for sec, ed in allowed_sections.items():
            if ed is None:
                continue
            raw[sec] = {
                (norm(k) if str(k).strip() else ""): str(v).strip()
                for k, v in ed.items()
                if str(k).strip()
            }

        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)

        return backup
    except Exception:
        # Surface to caller (manager shows error dialog); any backup created before failure is left on disk.
        raise


def resolve_learned_mappings_path(config_path: Path, cfg: Optional["org.Config"] = None) -> Path:
    """Pure (testable, non-Tk) helper for Phase 3c: resolve the learned mappings JSON path
    using *exactly* the same logic as org.load_learned_franchises / write_learned_franchises
    (prefer _loaded_config_path sibling when relative learned_franchises_file is set on cfg;
    else sibling to provided config_path). Exposed for manager + pure tests. No changes to r34_organizer.py.
    """
    if config_path is None:
        return Path("learned_character_franchises.json")
    fname = "learned_character_franchises.json"
    if cfg is not None:
        try:
            lfile = getattr(cfg, "learned_franchises_file", None) or fname
            fname = Path(lfile).name
            loaded = getattr(cfg, "_loaded_config_path", None)
            if loaded:
                return Path(loaded).with_name(fname)
        except Exception:
            pass
    # Fallback: sibling to the config file (as done for GUI --config case)
    return Path(config_path).parent / fname


def apply_learned_mappings_edits(learned_path: Path, edits: Optional[dict] = None) -> Optional[Path]:
    """Pure (non-Tk), testable helper for Phase 3c learned mappings edit (norm char key -> franchise folder).

    - If learned_path exists: ALWAYS create collision-proof backup
      learned_character_franchises.backup.YYYYMMDD-HHMMSS-ffffff.json (via shutil.copy2) BEFORE write.
    - If not exist: create the file (no prior backup possible; caller documents in UI "created, no backup").
    - Keys normalized via org.normalize (or identical fallback); values .strip() (ws cleanup, preserve casing).
    - Writes *only* the {normkey: folder, ...} dict (never merges into r34_config.json or char_* sections).
    - Creates parent dirs if needed.
    - Returns backup Path if one was created (existing case), else None (new file).
    - If copy2 backup fails: do not write (hard requirement, like apply_known...).
    - Reuses %f + "backup before write" pattern from apply_known_values_edits_to_config (3b + safety patch).

    Called from manager Save (after in-mem pending) and direct from pure 3c tests.
    """
    if learned_path is None:
        return None
    p = Path(learned_path)
    edits = edits or {}
    try:
        backup = None
        if p.exists():
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = p.with_name(f"{p.stem}.backup.{ts}.json")
            shutil.copy2(str(p), str(backup))  # collision-proof backup BEFORE any write

        p.parent.mkdir(parents=True, exist_ok=True)

        # same norm as 3a/3b/config pure + org
        if org is not None and hasattr(org, "normalize"):
            norm = org.normalize
        else:
            def norm(x: object) -> str:
                if x is None:
                    return ""
                value = str(x).lower()
                value = value.replace("&", " and ")
                value = value.replace("'", "")
                value = re.sub(r"[^a-z0-9]+", " ", value)
                return re.sub(r"\s+", " ", value).strip()

        to_write = {
            (norm(k) if str(k).strip() else ""): str(v).strip()
            for k, v in edits.items()
            if str(k).strip()
        }
        p.write_text(json.dumps(to_write, indent=2, ensure_ascii=False), encoding="utf-8")
        return backup
    except Exception:
        # Surface; backup (if made) left on disk
        raise


# Phase 3d pure (non-Tk) helpers for view-only dest folders + resolutions validation reports.
# No writes, no FS create/mutate, no config changes. Reusable in tests + manager refresh.
# Reuse 3c resolve_learned_mappings_path for learned cross-refs; org.build_reference_data for ref data (read-only).

def build_destination_folder_validation_report(config_path: Path) -> dict:
    """Pure helper for Phase 3d: build view data + validation for destination folders.

    - Scans via org.build_reference_data (existing dest subdirs under root).
    - Cross-refs targets from folder_aliases, character_mappings, learned (via resolve_learned + json).
    - For each pointed target: exists on disk? in ref? sources list.
    - Reports issues for missing targets (not exist or not in ref), with sources.
    - Returns structured dict for UI lists (no side effects).
    """
    if config_path is None:
        return {"error": "no config path", "folders": [], "issues": []}
    try:
        cpath = Path(config_path)
        cfg = org.load_config(cpath) if (org and cpath.exists()) else None
        dest_str = getattr(cfg, "destination_root", None) if cfg else None
        dest = Path(dest_str) if dest_str and Path(dest_str).exists() else Path(".")
        ref = org.build_reference_data(dest, cfg) if (org and dest.exists()) else None
        existing = (ref.destination_folders if ref else {})

        # load learned for cross-ref (reuse 3c pure, no org edit)
        learned = {}
        try:
            lp = resolve_learned_mappings_path(cpath, cfg)
            if lp.exists():
                raw = json.loads(lp.read_text(encoding="utf-8"))
                learned = {(org.normalize(k) if (org and hasattr(org, "normalize")) else str(k).lower().replace(" ", "")): v
                           for k, v in raw.items() if str(k).strip()}
        except Exception:
            learned = {}

        from collections import defaultdict
        pointed_sources = defaultdict(list)
        if cfg:
            for al, tgt in getattr(cfg, "folder_aliases", {}).items():
                pointed_sources[tgt].append(f"folder_alias:{al}")
            for ch, tgt in getattr(cfg, "character_mappings", {}).items():
                pointed_sources[tgt].append(f"char_map:{ch}")
        for ch, tgt in learned.items():
            pointed_sources[tgt].append(f"learned:{ch}")

        folders = []
        issues = []
        for tgt, srcs in pointed_sources.items():
            nrm = (org.normalize(tgt) if (org and hasattr(org, "normalize")) else str(tgt).lower().replace(" ", ""))
            disp = tgt
            on_disk = (dest / tgt).exists() if dest else False
            in_ref = nrm in existing
            folders.append({"norm": nrm, "display": disp, "exists": on_disk, "sources": list(srcs), "in_ref": in_ref})
            if not on_disk or not in_ref:
                issues.append(f"Missing target '{tgt}' (sources: {', '.join(srcs)}) - on disk: {on_disk}, in ref: {in_ref}")

        return {
            "dest_root": str(dest),
            "folders": folders,
            "issues": issues,
            "pointed_targets": len(pointed_sources),
        }
    except Exception as e:
        return {"error": str(e), "folders": [], "issues": [f"error: {e}"]}


def build_resolution_validation_report(config_path: Path) -> dict:
    """Pure helper for Phase 3d: build view data + basic validation for resolutions.

    Uses ref.naming_style (from build_reference_data on dest scan + filename labels).
    Reports sample count, labels, learned buckets, simple issues (e.g. low samples).
    No writes, no media probing beyond existing build scan (name parse only).
    """
    if config_path is None:
        return {"error": "no config path", "resolutions": {}, "issues": []}
    try:
        cpath = Path(config_path)
        cfg = org.load_config(cpath) if (org and cpath.exists()) else None
        dest_str = getattr(cfg, "destination_root", None) if cfg else None
        dest = Path(dest_str) if dest_str and Path(dest_str).exists() else Path(".")
        ref = org.build_reference_data(dest, cfg) if (org and dest.exists()) else None
        if not ref or not hasattr(ref, "naming_style"):
            return {"dest_root": str(dest), "resolutions": {}, "issues": ["no naming_style in ref"], "sample_count": 0}

        ns = ref.naming_style
        issues = []
        if getattr(ns, "sample_count", 0) < 1:
            issues.append("No resolution samples found during library scan.")
        return {
            "dest_root": str(dest),
            "resolutions": dict(getattr(ns, "resolution_labels", {})),
            "learned_buckets": list(getattr(ns, "learned_resolution_buckets", [])),
            "sample_count": getattr(ns, "sample_count", 0),
            "issues": issues,
        }
    except Exception as e:
        return {"error": str(e), "resolutions": {}, "issues": [f"error: {e}"], "sample_count": 0}


# ------------------------------------------------------------------
# Configuration / Helpers
# ------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

# Resolve the real location of the organizer and config.
# In frozen (PyInstaller) builds, __file__ points inside the bundle.
# We prefer files next to the executable when available (user can edit them).
if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
    # Running from built executable
    EXE_DIR = Path(sys.executable).resolve().parent
    # Prefer files next to the .exe over the ones inside _internal
    ORGANIZER_SCRIPT = EXE_DIR / "r34_organizer.py"
    DEFAULT_CONFIG = EXE_DIR / "r34_config.json"

    # Fall back to the bundled copies if the user didn't copy the .py next to the exe
    if not ORGANIZER_SCRIPT.exists():
        ORGANIZER_SCRIPT = SCRIPT_DIR / "r34_organizer.py"
    if not DEFAULT_CONFIG.exists():
        DEFAULT_CONFIG = SCRIPT_DIR / "r34_config.json"
else:
    ORGANIZER_SCRIPT = SCRIPT_DIR / "r34_organizer.py"
    DEFAULT_CONFIG = SCRIPT_DIR / "r34_config.json"


def find_ffprobe() -> Optional[str]:
    """Return path to ffprobe if available, else None."""
    return shutil.which("ffprobe")


def run_command(
    command: list[str],
    output_queue: "queue.Queue[str]",
    done_event: threading.Event,
) -> int:
    """
    Run a command in a thread, streaming stdout/stderr line-by-line into the queue.
    Returns the process return code.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=SCRIPT_DIR,
        )

        assert process.stdout is not None
        for line in process.stdout:
            output_queue.put(line.rstrip("\n"))

        return_code = process.wait()
        output_queue.put(f"\n[Process exited with code {return_code}]")
        return return_code
    except FileNotFoundError as e:
        output_queue.put(f"ERROR: {e}")
        return 127
    except Exception as e:
        output_queue.put(f"ERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        done_event.set()


# ------------------------------------------------------------------
# Lightweight Tkinter Tooltip (no external dependencies)
# Works in both source runs and PyInstaller frozen .exe builds.
# ------------------------------------------------------------------
class Tooltip:
    """Show a hover tooltip (balloon) with wrapped explanatory text for any widget.

    Usage:
        btn = ttk.Button(...)
        Tooltip(btn, "Detailed explanation of what this button does...")
    """

    def __init__(self, widget: tk.Widget, text: str, delay: int = 650, wraplength: int = 320):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wraplength = wraplength
        self.tip_window: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None

        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")  # hide immediately on click

    def _on_enter(self, event=None):
        self._cancel_pending()
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, event=None):
        self._cancel_pending()
        self._hide()

    def _cancel_pending(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self.tip_window or not self.text:
            return

        # Create floating tooltip window
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)  # no window decorations / title bar
        self.tip_window.attributes("-topmost", True)

        # Content
        frame = tk.Frame(self.tip_window, background="#ffffe0", borderwidth=1, relief="solid")
        frame.pack(ipadx=4, ipady=2)

        label = tk.Label(
            frame,
            text=self.text,
            justify=tk.LEFT,
            background="#ffffe0",
            foreground="#222222",
            font=("Segoe UI", 9),
            wraplength=self.wraplength,
            padx=6,
            pady=4,
        )
        label.pack()

        # Position just below the widget
        self.tip_window.update_idletasks()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4

        # Keep tooltip on screen (simple clamp)
        screen_w = self.widget.winfo_screenwidth()
        screen_h = self.widget.winfo_screenheight()
        tw = self.tip_window.winfo_width()
        th = self.tip_window.winfo_height()
        if x + tw > screen_w - 8:
            x = screen_w - tw - 8
        if y + th > screen_h - 8:
            y = self.widget.winfo_rooty() - th - 4  # show above instead

        self.tip_window.wm_geometry(f"+{x}+{y}")

    def _hide(self):
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None


class OrganizerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Rule34 Fresh Clip Organizer")
        self.root.minsize(900, 620)

        self.running = False
        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self.current_thread: Optional[threading.Thread] = None
        self.current_done_event: Optional[threading.Event] = None

        self._build_ui()
        self._check_ffprobe()

        # Helpful startup message for debugging path and frozen issues
        is_frozen = getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")
        mode = "FROZEN (built .exe)" if is_frozen else "SOURCE (python r34_gui.py)"
        self.append_output(f"[Startup] Running in {mode} mode")
        self.append_output(f"[Startup] Using organizer: {ORGANIZER_SCRIPT}")
        self.append_output(f"[Startup] Using config:    {DEFAULT_CONFIG}")

        # xAI API key status (secure - value never printed, controlled by toggle)
        if org is not None:
            try:
                config_path = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
                cfg = org.load_config(config_path)

                # Respect the per-config auto-load toggle
                auto_load = getattr(cfg, "auto_load_xai_key", True)
                if hasattr(self, "auto_load_xai_key_var"):
                    self.auto_load_xai_key_var.set(auto_load)

                if auto_load:
                    xai_key = org.get_xai_api_key(cfg, config_path=config_path)
                    status = "present (loaded from env or r34_xai_key.txt)" if xai_key else "not configured"
                    self.append_output(f"[Startup] xAI API key: {status}")
                    if hasattr(self, "xai_key_status"):
                        self.xai_key_status.set("Configured" if xai_key else "Not set")
                else:
                    self.append_output("[Startup] xAI API key: auto-load disabled (per config)")
                    if hasattr(self, "xai_key_status"):
                        self.xai_key_status.set("Auto-load disabled")
            except Exception as e:
                self.append_output(f"[Startup] xAI API key: error checking configuration ({e})")

        self._poll_output_queue()

    def _build_ui(self):
        # Top frame - paths
        path_frame = ttk.LabelFrame(self.root, text="Paths", padding=10)
        path_frame.pack(fill="x", padx=10, pady=(10, 5))

        # Config
        ttk.Label(path_frame, text="Config:").grid(row=0, column=0, sticky="e", padx=5)
        self.config_var = tk.StringVar(value=str(DEFAULT_CONFIG))
        ttk.Entry(path_frame, textvariable=self.config_var, width=70).grid(row=0, column=1, sticky="we", padx=5)
        self.config_var.trace_add("write", lambda *args: self._refresh_xai_key_status())
        btn_cfg = ttk.Button(path_frame, text="Browse...", command=self._browse_config)
        btn_cfg.grid(row=0, column=2)
        Tooltip(btn_cfg, "Select the r34_config.json file that defines your destination library root, character-to-franchise mappings, audio credits to strip (audiodude, evilaudio, multiaudio, etc.), junk tokens, AI/Grok settings, and the learned franchises file. This file is passed with --config to every preview/apply/undo operation so the organizer uses exactly the rules you maintain.")

        # Source folder
        ttk.Label(path_frame, text="Source folder:").grid(row=1, column=0, sticky="e", padx=5)
        self.source_var = tk.StringVar()
        ttk.Entry(path_frame, textvariable=self.source_var, width=70).grid(row=1, column=1, sticky="we", padx=5)
        btn_src = ttk.Button(path_frame, text="Browse...", command=self._browse_source)
        btn_src.grid(row=1, column=2)
        Tooltip(btn_src, "Select the folder containing your fresh Rule34 collector downloads (e.g. Akiryo/Audio Collection or any batch of messy .mp4 files). Run Preview will recursively scan only .mp4 files here (respecting your config), skipping any _r34_review quarantine folders. This is the input for the mandatory first phase of the safe two-step workflow.")

        # Destination root (optional override)
        ttk.Label(path_frame, text="Dest root (optional):").grid(row=2, column=0, sticky="e", padx=5)
        self.dest_var = tk.StringVar()
        ttk.Entry(path_frame, textvariable=self.dest_var, width=70).grid(row=2, column=1, sticky="we", padx=5)
        btn_dst = ttk.Button(path_frame, text="Browse...", command=self._browse_dest)
        btn_dst.grid(row=2, column=2)
        Tooltip(btn_dst, "Optional override for the root of your organized library (where clips will be moved as 'Artist - Character - Title [Res].mp4'). Leave blank to use the destination_root value from the selected config file. Handy when testing against a copy of your real collection without editing the JSON.")

        # xAI / Grok API Key management (secure - for AI-assisted result generation)
        ttk.Label(path_frame, text="xAI API Key:").grid(row=3, column=0, sticky="e", padx=5)
        self.xai_key_status = tk.StringVar(value="Not set")
        ttk.Label(path_frame, textvariable=self.xai_key_status, foreground="#666666").grid(row=3, column=1, sticky="w", padx=5)
        btn_xai = ttk.Button(path_frame, text="Set / Update...", command=self._set_xai_api_key)
        btn_xai.grid(row=3, column=2)
        Tooltip(btn_xai, "Securely set your xAI API key (the one used for Grok calls during preview for unknown characters/franchises). Stored only in r34_xai_key.txt next to your config. Never displayed after saving, never in config JSON, gitignored so it cannot leak in releases or shared builds.")

        # Optional toggle to control auto-loading the xAI key on startup (user privacy preference)
        self.auto_load_xai_key_var = tk.BooleanVar(value=True)
        chk = ttk.Checkbutton(
            path_frame,
            text="Auto-load xAI API key",
            variable=self.auto_load_xai_key_var,
            command=self._on_auto_load_xai_key_toggled
        )
        chk.grid(row=4, column=1, sticky="w", padx=5, pady=(2, 0))
        Tooltip(chk, "When enabled, the GUI will automatically attempt to load the xAI API key from the configured env var or r34_xai_key.txt on startup and show its status. Disable this if you prefer to never have the GUI read the key file automatically.")

        path_frame.columnconfigure(1, weight=1)

        # Action buttons
        button_frame = ttk.Frame(self.root, padding=5)
        button_frame.pack(fill="x", padx=10, pady=5)

        self.btn_preview = ttk.Button(button_frame, text="Run Preview", command=self.run_preview, width=18)
        self.btn_preview.pack(side="left", padx=5)
        Tooltip(self.btn_preview, "MANDATORY FIRST STEP (two-phase safety). Recursively scans the Source folder for videos, runs artist/character/franchise inference (heuristics + your config mappings + optional Grok AI), extracts real resolution via ffprobe, and writes a timestamped r34_preview_*.csv + matching .md report. NO files are ever moved or renamed during preview. Review the CSV in Excel or the Correction Tool (especially the 'approved' column), then run Apply. Console Output also shows a clean summary of original vs. proposed names.")

        self.btn_select_csv = ttk.Button(button_frame, text="Select Reviewed CSV...", command=self.select_csv, width=20)
        self.btn_select_csv.pack(side="left", padx=5)
        Tooltip(self.btn_select_csv, "Choose a previously generated r34_preview_*.csv that you (or the Correction Tool) have already reviewed and edited. The selected file becomes the active plan for 'Apply Approved Plan' and is auto-suggested to the Correction Tool and Undo. The GUI remembers it so Undo can automatically locate the matching r34_apply_*.csv log next to it.")

        self.btn_apply = ttk.Button(button_frame, text="Apply Approved Plan", command=self.run_apply, width=20)
        self.btn_apply.pack(side="left", padx=5)
        Tooltip(self.btn_apply, "THE ONLY BUTTON THAT MOVES FILES. Processes the selected reviewed preview CSV and moves/quarantines ONLY rows where approved=yes (or true/1). Respects blocked/content_review statuses, never overwrites existing targets, creates destination folders as needed. On every successful 'moved' row the character → target_folder decision is automatically persisted to learned_character_franchises.json so future previews become smarter. Always produces a dated r34_apply_*.csv log (visible in the apply log prompt) that powers the Undo button.")

        self.btn_correct = ttk.Button(button_frame, text="Open Correction Tool", command=self.open_correction_tool, width=20)
        self.btn_correct.pack(side="left", padx=5)
        Tooltip(self.btn_correct, "Opens an interactive table editor for the current or latest preview CSV. Lets you manually override target_folder, target_filename, and notes for any row (great for fixing difficult collector filenames like the Akiryo 'Mai...' batch or weak Grok results). 'Apply Correction' writes the edit with a full audit trail (manual_correction timestamp in notes + reason). 'Mark as Approved' flips the flag. Finally Save writes the CSV so the corrected plan is ready for Apply. All changes stay human-visible in the final apply log.")

        self.btn_undo = ttk.Button(button_frame, text="Undo Last Apply", command=self.run_undo, width=18)
        self.btn_undo.pack(side="left", padx=5)
        Tooltip(self.btn_undo, "Complete safety net. Reverses every file move recorded in the matching r34_apply_*.csv log (auto-detected from your last plan, or you can browse for any apply log). Files are moved back to their exact original source locations; on conflict they are quarantined instead of overwritten. Also fully undoes any character→franchise learning that the corresponding Apply had committed. The console and resulting undo log show exactly what was restored and which learning entries were rolled back.")

        self.btn_open_folder = ttk.Button(button_frame, text="Open Output Folder", command=self.open_output_folder)
        self.btn_open_folder.pack(side="right", padx=5)
        Tooltip(self.btn_open_folder, "Quickly opens Windows Explorer on your Source folder (the one containing the preview CSVs, apply/undo logs, and any _r34_review quarantine folders created during Apply). Falls back to the folder containing the GUI script if no source is selected. Handy for manually inspecting the artifacts the organizer produces.")

        # Output console
        console_frame = ttk.LabelFrame(self.root, text="Console Output", padding=5)
        console_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.console = scrolledtext.ScrolledText(console_frame, height=20, wrap="word", state="disabled")
        self.console.pack(fill="both", expand=True)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w")
        status_bar.pack(fill="x", side="bottom", padx=5, pady=(0, 5))

        # Make console readable
        self.console.tag_config("error", foreground="red")
        self.console.tag_config("success", foreground="green")

    def _check_ffprobe(self):
        if not find_ffprobe():
            self.append_output(
                "WARNING: ffprobe not found on PATH. Resolution detection will fail.\n"
                "Please install ffmpeg (ffprobe) and add it to your system PATH.",
                tag="error",
            )

    def append_output(self, text: str, tag: Optional[str] = None):
        self.console.configure(state="normal")
        if tag:
            self.console.insert("end", text + "\n", tag)
        else:
            self.console.insert("end", text + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")
        self.root.update_idletasks()

    def _poll_output_queue(self):
        try:
            while True:
                line = self.output_queue.get_nowait()
                self.append_output(line)
        except queue.Empty:
            pass

        if self.running:
            self.root.after(80, self._poll_output_queue)
        else:
            # Final drain + re-enable
            try:
                while True:
                    line = self.output_queue.get_nowait()
                    self.append_output(line)
            except queue.Empty:
                pass
            self._set_buttons_enabled(True)
            self.status_var.set("Ready")
            self._handle_command_completion()

    def _set_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.btn_preview.config(state=state)
        self.btn_select_csv.config(state=state)
        self.btn_apply.config(state=state)
        self.btn_correct.config(state=state)
        self.btn_undo.config(state=state)

    def _browse_config(self):
        path = filedialog.askopenfilename(
            title="Select config file",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=self.config_var.get(),
        )
        if path:
            self.config_var.set(path)

    def _browse_source(self):
        path = filedialog.askdirectory(title="Select source folder to preview")
        if path:
            self.source_var.set(path)

    def _browse_dest(self):
        path = filedialog.askdirectory(title="Select destination root (optional override)")
        if path:
            self.dest_var.set(path)

    def _set_xai_api_key(self):
        """Securely set or update the xAI API key used for Grok calls during preview.

        The key is written ONLY to r34_xai_key.txt next to the config.
        It is never stored in the JSON config, never shown in the UI after saving,
        and the file is gitignored so it cannot leak into releases.
        This is the auth token the tool uses to call X AI for help with result generation.
        """
        if org is None:
            messagebox.showerror("Error", "Organizer module not available.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Set xAI / Grok API Key")
        dialog.geometry("480x170")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="Paste your xAI API key (starts with xai- or sk-):").pack(padx=10, pady=(10, 5), anchor="w")

        key_var = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=key_var, width=55, show="*")
        entry.pack(padx=10, pady=5)
        entry.focus_set()

        def save_key():
            key = key_var.get().strip()
            if not key:
                messagebox.showwarning("Empty", "No key entered.")
                return

            try:
                cfg_path = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
                key_file = cfg_path.with_name("r34_xai_key.txt")
                key_file.write_text(key + "\n", encoding="utf-8")
                self.xai_key_status.set("Configured")
                messagebox.showinfo("Saved", "xAI API key saved securely to r34_xai_key.txt\n\nThis file is gitignored and will not be included in any releases or shared builds.")
                dialog.destroy()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save key: {e}")

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Save Securely", command=save_key).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side="left", padx=5)

        # When user sets a key, it's reasonable to turn auto-load back on
        self.auto_load_xai_key_var.set(True)
        self._refresh_xai_key_status()

    def _on_auto_load_xai_key_toggled(self):
        """Called when the user toggles the Auto-load xAI API key checkbox."""
        enabled = self.auto_load_xai_key_var.get()
        self._refresh_xai_key_status()

        # Persist preference to the current config JSON (best effort)
        try:
            config_path = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
            if config_path.exists():
                try:
                    data = json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                data["auto_load_xai_key"] = enabled
                config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass  # Non-fatal

    def _refresh_xai_key_status(self):
        """Re-check and update the xAI key status label based on current toggle + config."""
        if org is None or not hasattr(self, "xai_key_status"):
            return
        try:
            config_path = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
            cfg = org.load_config(config_path)

            if not self.auto_load_xai_key_var.get():
                self.xai_key_status.set("Auto-load disabled")
                return

            key = org.get_xai_api_key(cfg, config_path=config_path)
            self.xai_key_status.set("Configured" if key else "Not set")
        except Exception:
            self.xai_key_status.set("Error checking")

    def _get_base_command(self) -> list[str]:
        """Return the python + script prefix used by the existing launchers.

        CRITICAL: When the GUI is running as a PyInstaller-built executable,
        we must NEVER use sys.executable in the command. Doing so frequently
        causes Windows + PyInstaller to spawn another full copy of the GUI.
        """
        organizer = str(ORGANIZER_SCRIPT)

        # Detect if we are inside a PyInstaller bundle
        is_frozen = getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")

        if is_frozen:
            # We are running the built .exe → must use a real system Python
            candidates = ["python", "python3", "py"]
            for name in candidates:
                exe = shutil.which(name)
                if exe:
                    if name == "py":
                        return [exe, "-3", organizer]
                    return [exe, organizer]

            raise RuntimeError(
                "No Python interpreter found on your system PATH.\n\n"
                "The GUI needs a real Python installation to run the organizer.\n"
                "Please install Python 3.10+ and ensure 'python' or 'py' works in Command Prompt."
            )

        # Normal development run (python r34_gui.py)
        return [sys.executable, organizer]

    def run_preview(self):
        source = self.source_var.get().strip()
        if not source:
            messagebox.showerror("Error", "Please select a source folder.")
            return
        if not Path(source).is_dir():
            messagebox.showerror("Error", "Source path is not a valid folder.")
            return

        config = self.config_var.get().strip() or str(DEFAULT_CONFIG)
        dest = self.dest_var.get().strip()

        try:
            base_cmd = self._get_base_command()
        except RuntimeError as e:
            messagebox.showerror("Python Required", str(e))
            return

        # Important: --config must come BEFORE the subcommand (preview)
        cmd = base_cmd + ["--config", config, "preview", "--source", source]
        if dest:
            cmd += ["--dest-root", dest]

        self._start_command(cmd, "Running preview...")

    def select_csv(self):
        path = filedialog.askopenfilename(
            title="Select reviewed preview CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.reviewed_csv_path = path
            self.append_output(f"Selected reviewed plan: {path}")
            # Auto-fill for apply if possible
            self.selected_plan = path

    def run_apply(self):
        plan = getattr(self, "selected_plan", None) or getattr(self, "reviewed_csv_path", None)
        if not plan:
            plan = filedialog.askopenfilename(
                title="Select reviewed preview CSV to apply",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            )
            if not plan:
                return
            self.selected_plan = plan

        config = self.config_var.get().strip() or str(DEFAULT_CONFIG)

        try:
            base_cmd = self._get_base_command()
        except RuntimeError as e:
            messagebox.showerror("Python Required", str(e))
            return

        # Important: --config must come BEFORE the subcommand (apply)
        cmd = base_cmd + ["--config", config, "apply", "--plan", plan]

        self._start_command(cmd, "Applying approved plan...")

    def run_undo(self):
        """Run undo using the most recent apply log (or let user select one)."""
        plan = getattr(self, "selected_plan", None)

        # Try to find the most recent apply log next to the plan
        log_path = None
        if plan:
            plan_path = Path(plan)
            run_id = plan_path.stem.replace("r34_preview_", "")
            candidate = plan_path.parent / f"r34_apply_{run_id}.csv"
            if candidate.exists():
                log_path = str(candidate)

        if not log_path:
            log_path = filedialog.askopenfilename(
                title="Select an r34_apply_*.csv log to undo",
                filetypes=[("CSV files", "*.csv")]
            )
            if not log_path:
                return

        config = self.config_var.get().strip() or str(DEFAULT_CONFIG)

        try:
            base_cmd = self._get_base_command()
        except RuntimeError as e:
            messagebox.showerror("Python Required", str(e))
            return

        cmd = base_cmd + ["--config", config, "undo", "--log", log_path]

        self._start_command(cmd, "Undoing previous apply...")

    def _start_command(self, cmd: list[str], status_msg: str):
        if self.running:
            messagebox.showwarning("Busy", "A command is already running.")
            return

        self._set_buttons_enabled(False)
        self.running = True
        self.status_var.set(status_msg)
        self.append_output(f"\n=== {status_msg} ===")
        self.append_output("Command: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

        self.output_queue = queue.Queue()
        self.current_done_event = threading.Event()

        thread = threading.Thread(
            target=self._run_and_handle_completion,
            args=(cmd, self.output_queue, self.current_done_event),
            daemon=True,
        )
        self.current_thread = thread
        thread.start()

        self._poll_output_queue()

    def _run_and_handle_completion(
        self, cmd: list[str], q: "queue.Queue[str]", done: threading.Event
    ):
        try:
            rc = run_command(cmd, q, done)
            self.last_return_code = rc
            self.last_command = cmd
            if rc == 0:
                q.put("\n[SUCCESS] Command completed successfully.")
            else:
                q.put(f"\n[ERROR] Command failed with exit code {rc}.")
        finally:
            self.running = False

    def _handle_command_completion(self):
        """Called after a background command finishes (in main thread)."""
        if not hasattr(self, "last_command") or not hasattr(self, "last_return_code"):
            return

        cmd = self.last_command
        rc = self.last_return_code

        if rc != 0:
            return

        # Heuristic detection of what just finished
        if "preview" in cmd:
            self._post_preview_actions()
        elif "apply" in cmd:
            self._post_apply_actions()

    def _post_preview_actions(self):
        source = self.source_var.get().strip()
        if not source:
            return

        # Find the newest preview files in the source (or output dir if we supported it)
        try:
            source_path = Path(source)
            candidates = sorted(
                source_path.glob("r34_preview_*.csv"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                latest_csv = candidates[0]
                latest_md = latest_csv.with_suffix(".md")
                self.selected_plan = str(latest_csv)  # for convenient Apply later

                self.append_output(f"\nPreview artifacts created:")
                self.append_output(f"  CSV: {latest_csv}")
                if latest_md.exists():
                    self.append_output(f"  MD : {latest_md}")

                # Offer to open them
                if messagebox.askyesno(
                    "Preview Complete",
                    "Preview finished successfully.\n\nOpen the CSV plan for review?",
                ):
                    os.startfile(str(latest_csv))
                    if latest_md.exists():
                        os.startfile(str(latest_md))
        except Exception as e:
            self.append_output(f"Could not locate preview artifacts: {e}", tag="error")
            return

        # NEW: Also print the actual results that went into the CSV to the console
        self._print_csv_results_to_console(latest_csv)

    def _post_apply_actions(self):
        # Try to open the most recent apply log next to the plan
        plan = getattr(self, "selected_plan", None)
        if plan and Path(plan).exists():
            plan_path = Path(plan)
            run = plan_path.stem.replace("r34_preview_", "")
            log_path = plan_path.parent / f"r34_apply_{run}.csv"
            if log_path.exists():
                if messagebox.askyesno("Apply Complete", "Open the apply log?"):
                    os.startfile(str(log_path))

    def open_correction_tool(self):
        """Opens an integrated window for manually correcting filenames in a preview CSV."""
        if org is None:
            messagebox.showerror("Error", "Could not import r34_organizer module.")
            return

        plan = getattr(self, "selected_plan", None)
        if not plan or not Path(plan).exists():
            # Try to find the latest preview CSV in the source
            source = self.source_var.get().strip()
            if source:
                try:
                    source_path = Path(source)
                    candidates = sorted(source_path.glob("r34_preview_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if candidates:
                        plan = str(candidates[0])
                except Exception:
                    pass

        if not plan or not Path(plan).exists():
            plan = filedialog.askopenfilename(
                title="Select a preview CSV to correct",
                filetypes=[("CSV files", "*.csv")]
            )
            if not plan:
                return

        self.selected_plan = plan

        # Create correction window
        win = tk.Toplevel(self.root)
        win.title("Filename Correction Tool")
        win.geometry("1100x650")

        # Load rows
        try:
            rows = org.read_csv(Path(plan))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read CSV: {e}")
            win.destroy()
            return

        self.correction_rows = rows  # keep reference
        self.correction_plan_path = plan

        # Top info
        info_frame = ttk.Frame(win, padding=8)
        info_frame.pack(fill="x")
        ttk.Label(info_frame, text=f"Editing: {Path(plan).name}", font=("Segoe UI", 11, "bold")).pack(anchor="w")

        # Treeview for files
        tree_frame = ttk.Frame(win)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("check", "original", "artist", "character", "current_target", "status", "approved")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree.heading("check", text="✓", anchor="center")
        tree.heading("original", text="Original Filename")
        tree.heading("artist", text="Artist")
        tree.heading("character", text="Character")
        tree.heading("current_target", text="Current Target")
        tree.heading("status", text="Status")
        tree.heading("approved", text="Approved")

        tree.column("check", width=30, minwidth=30, stretch=False, anchor="center")
        tree.column("original", width=300)
        tree.column("artist", width=120)
        tree.column("character", width=140)
        tree.column("current_target", width=260)
        tree.column("status", width=90)
        tree.column("approved", width=80)

        # Sorting (click headers)
        tree.heading("status", command=lambda: self._sort_tree("status"))
        tree.heading("current_target", command=lambda: self._sort_tree("current_target"))
        tree.heading("character", command=lambda: self._sort_tree("character"))
        tree.heading("original", command=lambda: self._sort_tree("original"))

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Populate tree
        for i, row in enumerate(rows):
            if "_bulk_checked" not in row:
                row["_bulk_checked"] = False
            checked = "☑" if row.get("_bulk_checked") else "☐"
            orig = row.get("original_name", "")
            artist = row.get("artist", "")
            char = row.get("character", "")
            folder = row.get("target_folder", "")
            fname = row.get("target_filename", "")
            current_target = f"{folder}/{fname}" if folder and fname else (fname or "")
            status = row.get("status", "")
            approved = row.get("approved", "")
            tree.insert("", "end", iid=str(i), values=(checked, orig, artist, char, current_target, status, approved))

        self.correction_tree = tree
        self.correction_rows = rows

        # P1 Phase 1: load known values (structured, per constraints) for read-only dropdowns.
        # Dropdown select loads value only; explicit Apply buttons (added below) perform changes.
        try:
            cpath = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
            self.correction_known = get_known_values(cpath)
        except Exception:
            self.correction_known = {"artists": [], "franchises": [], "characters": [], "resolutions": []}

        # Edit panel
        edit_frame = ttk.LabelFrame(win, text="Correct Selected Row(s)", padding=10)
        edit_frame.pack(fill="x", padx=10, pady=8)
        self.edit_frame = edit_frame  # for dynamic label updates on multi-select

        ttk.Label(edit_frame, text="Corrected Target Folder:").grid(row=0, column=0, sticky="e", padx=5)
        self.corr_folder_var = tk.StringVar()
        ttk.Entry(edit_frame, textvariable=self.corr_folder_var, width=40).grid(row=0, column=1, sticky="w")

        ttk.Label(edit_frame, text="Corrected Target Filename:").grid(row=1, column=0, sticky="e", padx=5)
        self.corr_filename_var = tk.StringVar()
        ttk.Entry(edit_frame, textvariable=self.corr_filename_var, width=50).grid(row=1, column=1, sticky="w")

        ttk.Label(edit_frame, text="Notes:").grid(row=2, column=0, sticky="e", padx=5)
        self.corr_notes_var = tk.StringVar()
        ttk.Entry(edit_frame, textvariable=self.corr_notes_var, width=50).grid(row=2, column=1, sticky="w")

        # Buttons
        btn_frame = ttk.Frame(edit_frame)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)

        btn_apply_corr = ttk.Button(btn_frame, text="Apply Correction to Selected Row(s)", command=self._apply_correction)
        btn_apply_corr.pack(side="left", padx=5)
        Tooltip(btn_apply_corr, "Applies the values from the edit fields to ALL currently selected rows (supports Ctrl+Click / Shift+Click multi-select). Only non-empty fields are applied (so you can change just the folder for a batch, for example). Changes are in-memory; use Save All Changes to persist. A correction note is appended to each affected row for audit.")

        btn_mark = ttk.Button(btn_frame, text="Mark as Approved", command=self._mark_selected_approved)
        btn_mark.pack(side="left", padx=5)
        Tooltip(btn_mark, "Sets approved='yes' on the currently selected row (identical to editing the CSV by hand). After marking, the row will be included when you run Apply Approved Plan from the main window. Use this after you have reviewed or manually corrected a difficult filename so it is no longer skipped.")

        btn_reset = ttk.Button(btn_frame, text="Reset Selected Row", command=self._reset_selected_row)
        btn_reset.pack(side="left", padx=5)
        Tooltip(btn_reset, "Placeholder action (currently shows an info dialog). A future enhancement could reload the original values for the selected row from a hidden backup copy of the preview CSV. For now, simply close this Correction Tool window without saving and re-open it from the original preview CSV if you want to discard all edits.")

        # Bulk actions
        bulk_frame = ttk.Frame(edit_frame)
        bulk_frame.grid(row=4, column=0, columnspan=2, pady=(5, 0))
        btn_approve_sel = ttk.Button(bulk_frame, text="Approve Selected (Ctrl/Shift+Click)", command=self._approve_selected)
        btn_approve_sel.pack(side="left", padx=5)
        Tooltip(btn_approve_sel, "Marks all currently selected (highlighted) rows (use Ctrl+Click or Shift+Click in the table) as approved='yes'. Complements the checkbox system — you can also right-click highlighted rows and use 'Select multiple (toggle checkboxes)' if you prefer the checkmark workflow.")

        btn_approve_chk = ttk.Button(bulk_frame, text="Approve Checked (☑)", command=self._approve_checked)
        btn_approve_chk.pack(side="left", padx=5)
        Tooltip(btn_approve_chk, "Marks all rows that have a checkmark in the ✓ column as approved='yes'. Click the checkbox column (first column) to toggle individual rows. Shift/Ctrl+Click to highlight multiple rows, then right-click and choose 'Select multiple (toggle checkboxes)' to batch-toggle their checkmarks — now the highlighted rows behave like individually checked ones.")

        # Bind selection and checkbox toggle
        tree.bind("<<TreeviewSelect>>", self._on_correction_row_selected)
        tree.bind("<ButtonRelease-1>", self._on_correction_tree_click, add="+")

        # Right-click context menu so that Shift/Ctrl+Click row highlighting
        # can drive the checkbox column (same effect as clicking individual ✓ cells).
        # User highlights a batch (or many), right-clicks anywhere, chooses the item
        # to batch-toggle those rows' _bulk_checked state + the visible symbol.
        context_menu = tk.Menu(tree, tearoff=0)
        context_menu.add_command(
            label="Select multiple (toggle checkboxes)",
            command=self._toggle_checkboxes_for_selected
        )
        self.correction_context_menu = context_menu

        tree.bind("<Button-3>", self._on_correction_tree_right_click)

        # P1 Phase 1: Known-value dropdowns at bottom with buttons (read-only load per constraints).
        # Select in cb ONLY loads value (self._last_picked_* or populates edit field for preview).
        # User must click explicit Apply* button to modify selected rows (Franchise/Folder required to apply to rows;
        # Artist/Char/Res conservative: populate edit field if part-replace uncertain).
        picks_frame = ttk.LabelFrame(win, text="Quick Pick Known Values (select loads; click Apply to use on selected rows)", padding=6)
        picks_frame.pack(fill="x", padx=10, pady=(0, 8))

        # Artists
        ttk.Label(picks_frame, text="Artists:").grid(row=0, column=0, sticky="e", padx=3)
        self.corr_artist_cb = ttk.Combobox(picks_frame, values=self.correction_known.get("artists", []), width=18, state="readonly")
        self.corr_artist_cb.grid(row=0, column=1, sticky="w")
        self.corr_artist_cb.bind("<<ComboboxSelected>>", lambda e: self._on_known_pick("artist", self.corr_artist_cb.get()))
        ttk.Button(picks_frame, text="Apply Artist", command=lambda: self._apply_picked_value("artist")).grid(row=0, column=2, padx=3)

        # Franchises (required Apply to rows)
        ttk.Label(picks_frame, text="Franchises/Folders:").grid(row=0, column=3, sticky="e", padx=3)
        self.corr_franchise_cb = ttk.Combobox(picks_frame, values=self.correction_known.get("franchises", []), width=18, state="readonly")
        self.corr_franchise_cb.grid(row=0, column=4, sticky="w")
        self.corr_franchise_cb.bind("<<ComboboxSelected>>", lambda e: self._on_known_pick("franchise", self.corr_franchise_cb.get()))
        ttk.Button(picks_frame, text="Apply Franchise to Rows", command=lambda: self._apply_picked_value("franchise")).grid(row=0, column=5, padx=3)

        # Characters
        ttk.Label(picks_frame, text="Characters:").grid(row=1, column=0, sticky="e", padx=3)
        self.corr_character_cb = ttk.Combobox(picks_frame, values=self.correction_known.get("characters", []), width=18, state="readonly")
        self.corr_character_cb.grid(row=1, column=1, sticky="w")
        self.corr_character_cb.bind("<<ComboboxSelected>>", lambda e: self._on_known_pick("character", self.corr_character_cb.get()))
        ttk.Button(picks_frame, text="Apply Character", command=lambda: self._apply_picked_value("character")).grid(row=1, column=2, padx=3)

        # Resolutions
        ttk.Label(picks_frame, text="Resolutions:").grid(row=1, column=3, sticky="e", padx=3)
        self.corr_resolution_cb = ttk.Combobox(picks_frame, values=self.correction_known.get("resolutions", []), width=18, state="readonly")
        self.corr_resolution_cb.grid(row=1, column=4, sticky="w")
        self.corr_resolution_cb.bind("<<ComboboxSelected>>", lambda e: self._on_known_pick("resolution", self.corr_resolution_cb.get()))
        ttk.Button(picks_frame, text="Apply Resolution", command=lambda: self._apply_picked_value("resolution")).grid(row=1, column=5, padx=3)

        ttk.Button(picks_frame, text="Refresh Lists", command=self._refresh_known_lists).grid(row=0, column=6, rowspan=2, padx=6)
        ttk.Button(picks_frame, text="Manage Known Values...", command=self._open_known_values_manager).grid(row=0, column=7, rowspan=2, padx=3)  # stub for P3

        # Bottom bar
        bottom = ttk.Frame(win, padding=8)
        bottom.pack(fill="x")
        btn_save = ttk.Button(bottom, text="Save All Changes to CSV", command=self._save_corrections)
        btn_save.pack(side="right")
        Tooltip(btn_save, "Writes the entire in-memory table (including every manual correction, approval change, and note you made) back to the original preview CSV file on disk using the same format the organizer expects. After a successful save the Correction window closes automatically. You can then immediately click 'Apply Approved Plan' in the main window — all your edits will be honored and any new character→folder pairs from corrected rows will be learned on success. The audit tags you added are preserved in the CSV so they appear in the final apply log.")
        ttk.Label(bottom, text="Corrections will be visible in the reason/notes when you Apply.").pack(side="left")

        self.correction_window = win

    def _approve_row(self, row: dict, iid: str = None, tree=None):
        """Internal: mark a row approved and, for fixable statuses (e.g. unmatched after correction),
        also flip status to 'ready' so it will be applied (instead of treated as blocked).
        """
        row["approved"] = "yes"
        if iid and tree:
            try:
                tree.set(iid, "approved", "yes")
            except Exception:
                pass
        cur = (row.get("status") or "").strip().lower()
        if cur in ("unmatched", "duplicate", "blocked", "invalid", "") or cur == "needs review":
            row["status"] = "ready"
            if iid and tree:
                try:
                    tree.set(iid, "status", "ready")
                except Exception:
                    pass

    def _on_correction_row_selected(self, event=None):
        """Load the first selected row into the edit fields as a template.
        When multiple rows are selected, edits will apply to all of them on 'Apply Correction'."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            return

        # Update the edit panel label to reflect multi-select
        if hasattr(self, "edit_frame") and self.edit_frame:
            count = len(selection)
            label = f"Correct Selected Rows ({count}) — edits apply to ALL selected" if count > 1 else "Correct Selected Row"
            self.edit_frame.config(text=label)

        iid = selection[0]
        idx = int(iid)
        row = self.correction_rows[idx]

        self.corr_folder_var.set(row.get("target_folder", ""))
        self.corr_filename_var.set(row.get("target_filename", ""))
        self.corr_notes_var.set(row.get("notes", ""))

    def _on_correction_tree_click(self, event):
        """Handle click on checkbox column to toggle bulk selection."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        region = tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = tree.identify_column(event.x)
        item = tree.identify_row(event.y)
        if not item or col != "#1":  # first column is "check"
            return
        try:
            idx = int(item)
            row = self.correction_rows[idx]
            row["_bulk_checked"] = not row.get("_bulk_checked", False)
            sym = "☑" if row["_bulk_checked"] else "☐"
            tree.set(item, "check", sym)
        except (ValueError, IndexError, KeyError):
            pass

    def _on_correction_tree_right_click(self, event):
        """Show context menu on right-click (Windows Button-3).

        If the clicked row is not already part of the current multi-selection
        (made via Shift or Ctrl+Click), select it. Then the menu allows
        "Select multiple" to toggle the checkboxes on all currently highlighted rows.

        This makes visual row highlighting (extended selection) a first-class way
        to drive the checkbox column, exactly like individual checkbox clicks.
        """
        tree = getattr(self, "correction_tree", None)
        if not tree or not hasattr(self, "correction_context_menu"):
            return
        item = tree.identify_row(event.y)
        if item:
            # Preserve an existing multi-selection if the user right-clicks inside it.
            # Only change selection if the clicked row is outside the current set.
            try:
                if item not in tree.selection():
                    tree.selection_set(item)
            except Exception:
                pass
        # Popup the menu (command executes against whatever selection exists at click time)
        try:
            self.correction_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.correction_context_menu.grab_release()

    def _toggle_checkboxes_for_selected(self):
        """Toggle _bulk_checked (and the ✓/☐ symbol) for every row in the current
        Treeview selection (the highlighted rows from Shift/Ctrl+Click).

        This is the action behind the right-click 'Select multiple' menu item.
        It lets you highlight a batch exactly like you would with checkboxes,
        then batch-toggle their check state so Approve Checked / other bulk
        checkbox-driven features see them. Live in-place update, no popups.
        """
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            self.status_var.set("No rows selected (use Shift/Ctrl+Click to highlight first).")
            return
        count = 0
        for iid in selection:
            try:
                idx = int(iid)
                row = self.correction_rows[idx]
                row["_bulk_checked"] = not row.get("_bulk_checked", False)
                sym = "☑" if row["_bulk_checked"] else "☐"
                tree.set(iid, "check", sym)
                count += 1
            except (ValueError, IndexError, KeyError):
                pass
        if count:
            msg = f"Toggled checkboxes on {count} selected row(s) (live)."
            self.append_output(msg)
            self.status_var.set(msg)

    def _apply_correction(self):
        """Apply the edit fields to ALL selected rows (supports multi-select via Ctrl/Shift).

        - Only non-empty fields are applied (enables folder-only or filename-only bulk edits).
        - A correction note is appended to every affected row.
        - target_path is recomputed per row using the (updated) folder + filename for that row.
        - Changes are reflected LIVE in the tree immediately (no popups, no confirmation dialogs).
        """
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            self.status_var.set("No rows selected for correction.")
            return

        new_folder = self.corr_folder_var.get().strip()
        new_filename = self.corr_filename_var.get().strip()
        notes = self.corr_notes_var.get().strip()

        if not new_folder and not new_filename:
            self.status_var.set("Enter a folder and/or filename to apply.")
            return

        # Prepare dest_root once (used for target_path recompute)
        dest_root = self.dest_var.get().strip() or ""
        if not dest_root:
            try:
                cfg = org.load_config(Path(self.config_var.get() or DEFAULT_CONFIG))
                dest_root = str(cfg.destination_root)
            except Exception:
                dest_root = ""

        correction_note = f"manual_correction: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if notes:
            correction_note += f" - {notes}"

        updated_count = 0
        affected_iids = []

        # P2: smart dup numbering for multi filename edit (wired after pure helpers + tests).
        # Check only "rest of the selected files"; auto number per scheme (after sex or char); dialog if ambiguous.
        if new_filename and len(selection) > 1:
            sel_rows = []
            for iid in selection:
                try:
                    sel_rows.append(self.correction_rows[int(iid)])
                except Exception:
                    pass
            dup_info = detect_selected_duplicate_targets(sel_rows, new_filename, self.correction_rows)
            if dup_info.get("would_collide_within_selected"):
                # build numbered for the group
                base_for_num = new_filename
                point = choose_number_insertion_point(parse_target_filename_parts(base_for_num), base_for_num)
                if point == "ambiguous":
                    # dialog for clarification (as requested)
                    choice = messagebox.askquestion("Number placement?", "Ambiguous where to insert number for dups.\nUse after character name? (Yes=after char, No=before res)", icon="warning")
                    point = "after_character" if choice == "yes" else "before_res"
                variants = build_numbered_filename_variants(base_for_num, len(sel_rows), point, set(r.get("target_filename","") for r in self.correction_rows))
                for i, iid in enumerate(selection):
                    try:
                        idx = int(iid)
                        if i < len(variants):
                            self.correction_rows[idx]["target_filename"] = variants[i]
                    except Exception:
                        pass
                # note in status
                self.status_var.set(f"Auto-numbered {len(sel_rows)} files for dups (point: {point}).")

        for iid in selection:
            try:
                idx = int(iid)
                row = self.correction_rows[idx]

                changed = False
                if new_folder:
                    row["target_folder"] = new_folder
                    changed = True
                if new_filename:
                    # if we numbered above, it may already be set; still mark changed
                    row["target_filename"] = row.get("target_filename") or new_filename
                    changed = True

                if not changed:
                    continue

                # Recompute target_path for this row using its (possibly updated) folder + filename
                row_folder = row.get("target_folder", "")
                row_fname = row.get("target_filename", "")
                if dest_root and row_folder and row_fname:
                    target_path = str(Path(dest_root) / row_folder / row_fname)
                    row["target_path"] = target_path

                # Append audit note to this row
                existing_notes = row.get("notes", "")
                row["notes"] = f"{existing_notes}; {correction_note}".strip("; ")

                existing_reason = row.get("reason", "")
                row["reason"] = f"{existing_reason};manual_filename_correction".strip(";")

                # If correcting an unmatched (or similar) row by providing target, auto-promote to ready+approved
                # so bulk "apply correction" makes it apply-able instead of still blocked on status=unmatched.
                cur_stat = (row.get("status") or "").strip().lower()
                if (new_folder or new_filename) and cur_stat in ("unmatched", "needs review", "blocked", "invalid", ""):
                    row["status"] = "ready"
                    row["approved"] = "yes"

                # Live update this row in the tree immediately (7-tuple with check column)
                checked = "☑" if row.get("_bulk_checked") else "☐"
                approved_val = row.get("approved", "")
                current_target = f"{row_folder}/{row_fname}" if row_folder else row_fname
                tree.item(iid, values=(
                    checked,
                    row.get("original_name", ""),
                    row.get("artist", ""),
                    row.get("character", ""),
                    current_target,
                    row.get("status", ""),
                    approved_val
                ))

                updated_count += 1
                affected_iids.append(iid)
            except (ValueError, IndexError, KeyError):
                pass

        if updated_count > 0:
            msg = f"Applied correction to {updated_count} selected row(s) — live in tree. Use Save to persist to CSV."
            self.append_output(msg)
            self.status_var.set(msg)
        else:
            self.status_var.set("No rows were updated (edit fields were empty).")

    def _mark_selected_approved(self):
        """Mark currently selected row(s) approved (live, supports multi-select)."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            self.status_var.set("No rows selected to approve.")
            return
        count = 0
        first_name = ""
        for iid in selection:
            try:
                idx = int(iid)
                row = self.correction_rows[idx]
                self._approve_row(row, iid, tree)
                if not first_name:
                    first_name = row.get("original_name", "")
                count += 1
            except (ValueError, IndexError, KeyError):
                pass
        if count:
            msg = f"Marked {count} row(s) as approved (live)."
            self.append_output(msg)
            self.status_var.set(msg)

    def _approve_selected(self):
        """Approve all currently selected rows (live update, no popup)."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            self.status_var.set("No rows selected to approve.")
            return
        count = 0
        for iid in selection:
            try:
                idx = int(iid)
                row = self.correction_rows[idx]
                self._approve_row(row, iid, tree)
                count += 1
            except (ValueError, IndexError, KeyError):
                pass
        if count:
            msg = f"Marked {count} selected row(s) as approved (live in tree)."
            self.append_output(msg)
            self.status_var.set(msg)

    def _approve_checked(self):
        """Approve all checked (☑) rows (live update, no popup)."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        count = 0
        for i, row in enumerate(self.correction_rows):
            if row.get("_bulk_checked"):
                self._approve_row(row, str(i), tree)
                count += 1
        if count:
            msg = f"Marked {count} checked row(s) as approved (live in tree)."
            self.append_output(msg)
            self.status_var.set(msg)
        else:
            self.status_var.set("No checked rows to approve.")

    # P1 Phase 1 support (read-only dropdowns + explicit Apply; per constraints: no auto-mod on select)
    def _on_known_pick(self, category: str, value: str):
        """Load value only (no row modification). Populate relevant edit field for user preview/confirm."""
        if not value:
            return
        if category == "franchise":
            self.corr_folder_var.set(value)
        elif category == "artist":
            # Conservative: populate filename field (user can refine); do not rewrite rows
            current = self.corr_filename_var.get().strip()
            if current:
                self.corr_filename_var.set(value + " - " + current if " - " not in current else value + current)
            else:
                self.corr_filename_var.set(value + " - ")
        elif category == "character":
            current = self.corr_filename_var.get().strip()
            if current:
                self.corr_filename_var.set(current.replace(" - ", " - " + value + " - ", 1) if " - " in current else value + " - " + current)
            else:
                self.corr_filename_var.set(value + " - ")
        elif category == "resolution":
            current = self.corr_filename_var.get().strip()
            if current and "[" in current:
                # replace res tag conservatively
                import re
                new = re.sub(r"\[[^\]]+\]", f"[{value}]", current)
                self.corr_filename_var.set(new)
            else:
                self.corr_filename_var.set(current + f" [{value}]" if current else f"Title [{value}]")
        # Store last for explicit Apply buttons if needed
        setattr(self, f"_last_picked_{category}", value)

    def _apply_picked_value(self, category: str):
        """Explicit Apply (required). Franchise/Folder: apply to rows. Others: conservative (populate edit if uncertain part replace)."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            self.status_var.set("No rows selected.")
            return
        value = getattr(self, f"_last_picked_{category}", None) or (self.corr_franchise_cb.get() if category == "franchise" else None)
        if not value:
            self.status_var.set(f"No {category} value picked from dropdown.")
            return

        if category == "franchise":
            # Required: apply to target_folder on selected rows (like bulk correction)
            updated = 0
            for iid in selection:
                try:
                    idx = int(iid)
                    row = self.correction_rows[idx]
                    row["target_folder"] = value
                    row_folder = value
                    row_fname = row.get("target_filename", "")
                    if row_fname:
                        dest_root = self.dest_var.get().strip() or ""
                        if not dest_root:
                            try:
                                c = org.load_config(Path(self.config_var.get() or DEFAULT_CONFIG))
                                dest_root = str(c.destination_root)
                            except Exception:
                                dest_root = ""
                        if dest_root:
                            row["target_path"] = str(Path(dest_root) / row_folder / row_fname)
                    # live tree
                    checked = "☑" if row.get("_bulk_checked") else "☐"
                    approved_val = row.get("approved", "")
                    cur_target = f"{row_folder}/{row_fname}" if row_folder and row_fname else row_fname
                    tree.item(iid, values=(checked, row.get("original_name", ""), row.get("artist", ""), row.get("character", ""), cur_target, row.get("status", ""), approved_val))
                    updated += 1
                except Exception:
                    pass
            if updated:
                self.status_var.set(f"Applied franchise '{value}' to {updated} row(s).")
        else:
            # Conservative for Artist/Char/Resolution per approval notes: if uncertain about safe filename-part replacement,
            # just populate the edit field instead of rewriting rows.
            # For P1, always populate the filename field (user confirms before Apply Correction or explicit future).
            if category == "artist":
                self.corr_filename_var.set(value + " - " + (self.corr_filename_var.get() or "Title"))
            elif category == "character":
                self.corr_filename_var.set((self.corr_filename_var.get() or "Artist - ") + value + " - Title")
            elif category == "resolution":
                current = self.corr_filename_var.get() or "Title"
                import re
                if re.search(r"\[[^\]]+\]", current):
                    self.corr_filename_var.set(re.sub(r"\[[^\]]+\]", f"[{value}]", current))
                else:
                    self.corr_filename_var.set(current + f" [{value}]")
            self.status_var.set(f"Loaded {category} '{value}' into edit field (use Apply Correction or edit manually).")

    def _refresh_known_lists(self):
        try:
            cpath = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
            self.correction_known = get_known_values(cpath)
            self.corr_artist_cb["values"] = self.correction_known.get("artists", [])
            self.corr_franchise_cb["values"] = self.correction_known.get("franchises", [])
            self.corr_character_cb["values"] = self.correction_known.get("characters", [])
            self.corr_resolution_cb["values"] = self.correction_known.get("resolutions", [])
            self.status_var.set("Known values lists refreshed.")
        except Exception as e:
            self.status_var.set(f"Refresh failed: {e}")

    def _open_known_values_manager(self):
        # Phase 3d limited (per directive): 5 editable sections (3a/3b/3c: artist+folder aliases, char mappings, canon aliases, learned) + dest folders/resolutions as view-only validation tabs (3d only; no writes/FS/edit/save for them).
        # Collision-proof %f backups only for the 5 editable. Preserve ALL other keys/structure exactly. No config writes or FS ops from 3d views.
        # Refresh validation buttons rebuild in-memory reports only (reuse org.build_reference_data + 3c resolve_learned + new pure val helpers).
        # Do not implement full Phase 3 / dest management / res editing. Keep all prior editable behavior 100% unchanged.
        win = tk.Toplevel(self.root)
        win.title("Known Values Manager (Phase 3d limited: 5 editable + dest_folders/res view-only validation + %f backups for editable; stop per directive)")
        win.geometry("820x600")

        # Load structured dicts (not the flattened correction_known lists) for schema edit of the 4 allowed (3a/3b) + learned (3c only).
        cpath = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
        cfg = None
        try:
            if org is not None:
                cfg = org.load_config(cpath) if cpath.exists() else None
        except Exception:
            cfg = None
        self._edit_artist_aliases = dict(getattr(cfg, 'artist_aliases', {})) if cfg else {}
        self._edit_folder_aliases = dict(getattr(cfg, 'folder_aliases', {})) if cfg else {}
        self._edit_character_mappings = dict(getattr(cfg, 'character_mappings', {})) if cfg else {}
        self._edit_canonical_character_aliases = dict(getattr(cfg, 'canonical_character_aliases', {})) if cfg else {}

        # Phase 3c: load learned mappings from its own file (resolved rel to selected config via _loaded or sibling).
        # In-mem only until Save. No write on open. Uses pure resolve (duplicates org logic without editing org.py).
        self._edit_learned_mappings = {}
        try:
            learned_p = resolve_learned_mappings_path(cpath, cfg)
            if learned_p.exists():
                raw_l = json.loads(learned_p.read_text(encoding="utf-8"))
                self._edit_learned_mappings = {
                    (org.normalize(k) if (org and hasattr(org, "normalize")) else str(k).lower().replace(" ", "")): v
                    for k, v in raw_l.items() if str(k).strip()
                }
        except Exception:
            self._edit_learned_mappings = {}

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        for cat, label in [
            ("artists", "Artists (artist_aliases - editable Phase 3b)"),
            ("franchises", "Franchises/Folders (folder_aliases - editable Phase 3b)"),
            ("character_mappings", "Character Mappings (character_mappings - editable Phase 3b)"),
            ("canonical_character_aliases", "Canonical Character Aliases (canonical_character_aliases - editable Phase 3b)"),
            ("learned", "Learned Mappings (learned_character_franchises.json - editable Phase 3c)"),
            ("dest_folders", "Destination Folders (view-only Phase 3d)"),
            ("characters", "Characters (view-only: mappings+canonical+learned - Phase 3d)"),
            ("resolutions", "Resolutions (naming style - view-only Phase 3d)")
        ]:
            f = ttk.Frame(nb)
            nb.add(f, text=label)

            if cat in ("artists", "franchises", "character_mappings", "canonical_character_aliases", "learned"):
                # Schema-aware 2-field editor for the 4 allowed (3a/3b) + learned (3c only). Reuse 3a/3b pattern, no UI rewrite.
                if cat == "artists":
                    edit_d = self._edit_artist_aliases
                    key_label = "Alias (key):"
                    val_label = "Canonical / Folder:"
                elif cat == "franchises":
                    edit_d = self._edit_folder_aliases
                    key_label = "Alias (key):"
                    val_label = "Canonical / Folder:"
                elif cat == "character_mappings":
                    edit_d = self._edit_character_mappings
                    key_label = "Character alias/key:"
                    val_label = "Franchise/folder:"
                elif cat == "canonical_character_aliases":
                    edit_d = self._edit_canonical_character_aliases
                    key_label = "Character alias/key:"
                    val_label = "Canonical display name:"
                else:  # learned (Phase 3c)
                    edit_d = self._edit_learned_mappings
                    key_label = "Learned character/key:"
                    val_label = "Franchise/folder:"

                lst = tk.Listbox(f, height=12)
                lst.pack(fill="both", expand=True, padx=4, pady=2)

                def _repop_list():
                    lst.delete(0, "end")
                    for k in sorted(edit_d.keys()):
                        lst.insert("end", f"{k} -> {edit_d[k]}")

                _repop_list()

                frm = ttk.Frame(f)
                frm.pack(fill="x", padx=4, pady=4)
                ttk.Label(frm, text=key_label).pack(side="left")
                alias_ent = ttk.Entry(frm, width=22)
                alias_ent.pack(side="left", padx=2)
                ttk.Label(frm, text=val_label).pack(side="left")
                canon_ent = ttk.Entry(frm, width=22)
                canon_ent.pack(side="left", padx=2)

                def _do_add_or_update():
                    a = alias_ent.get().strip()
                    c = canon_ent.get().strip()
                    if not a or not c:
                        return
                    nk = org.normalize(a) if (org and hasattr(org, "normalize")) else a.lower().replace(" ", "")
                    edit_d[nk] = c
                    _repop_list()

                def _do_remove():
                    sel = lst.curselection()
                    if not sel:
                        return
                    line = lst.get(sel[0])
                    if " -> " in line:
                        k = line.split(" -> ", 1)[0]
                        edit_d.pop(k, None)
                        _repop_list()

                ttk.Button(frm, text="Add / Update", command=_do_add_or_update).pack(side="left", padx=4)
                ttk.Button(frm, text="Remove Selected", command=_do_remove).pack(side="left")

                if cat == "learned":
                    def _do_reload_learned():
                        try:
                            lp = resolve_learned_mappings_path(cpath, cfg)
                            new_d = {}
                            if lp.exists():
                                raw = json.loads(lp.read_text(encoding="utf-8"))
                                new_d = {(org.normalize(k) if (org and hasattr(org, "normalize")) else str(k).lower().replace(" ", "")): v for k, v in raw.items() if str(k).strip()}
                            edit_d.clear()
                            edit_d.update(new_d)
                            _repop_list()
                        except Exception as ex:
                            # non-fatal for reload
                            pass
                    ttk.Button(frm, text="Reload from disk", command=_do_reload_learned).pack(side="left", padx=4)

                note = "Phase 3c: edits here affect only the learned mappings file (resolved rel to selected config). In-memory until Save. Collision-proof backup created on Save if file existed. Does not touch r34_config.json or the 4 alias/mapping sections." if cat == "learned" else "Phase 3b/3c: edits here affect only the four allowed (artist_aliases/folder_aliases/character_mappings/canonical_character_aliases) or learned (3c). In-memory until Save. Backup created on Save. Other sections (incl. dest/res views in 3d) untouched."
                ttk.Label(f, text=note).pack(pady=2)
            else:
                # Phase 3d view-only tabs: dest_folders (new dedicated with validation) + characters + resolutions (improved).
                # No Add/Edit/Remove/Save for these; labels "View-only in Phase 3d"; refresh only rebuilds in-memory report (no FS/config writes).
                if cat == "dest_folders":
                    # Two lists: folders + validation issues. Refresh button rebuilds via pure helper.
                    lst_folders = tk.Listbox(f, height=8)
                    lst_folders.pack(fill="both", expand=True, padx=4, pady=2)
                    lst_issues = tk.Listbox(f, height=6)
                    lst_issues.pack(fill="both", expand=True, padx=4, pady=2)
                    # store for refresh (closure over these)
                    self._dest_folders_lst = lst_folders
                    self._dest_issues_lst = lst_issues

                    def _repop_dest():
                        lst_folders.delete(0, "end")
                        lst_issues.delete(0, "end")
                        try:
                            rep = build_destination_folder_validation_report(cpath)
                            for it in rep.get("folders", []):
                                lst_folders.insert("end", f"{it.get('norm','?')}: {it.get('display','?')} (exists:{it.get('exists')}, in_ref:{it.get('in_ref')}, srcs:{len(it.get('sources',[]))})")
                            for iss in rep.get("issues", []):
                                lst_issues.insert("end", iss)
                            if "error" in rep:
                                lst_issues.insert("end", f"ERR: {rep['error']}")
                        except Exception as ex:
                            lst_issues.insert("end", f"ERR: {ex}")

                    _repop_dest()

                    def _refresh_dest():
                        _repop_dest()
                        self.status_var.set("Destination folder validation refreshed (view-only, no writes).")

                    ttk.Button(f, text="Refresh Folder Validation", command=_refresh_dest).pack(pady=2)
                    ttk.Label(f, text="View-only in Phase 3d. Shows scanned dest folders (from ref) + cross-ref validation for missing targets from aliases/char_maps/learned. No FS ops, no config writes. 'Refresh' rebuilds in-memory only.").pack(pady=2)
                elif cat == "resolutions":
                    # Improved view: richer than simple known list; use pure report for labels + issues.
                    lst_res = tk.Listbox(f, height=8)
                    lst_res.pack(fill="both", expand=True, padx=4, pady=2)
                    lst_res_issues = tk.Listbox(f, height=4)
                    lst_res_issues.pack(fill="both", expand=True, padx=4, pady=2)
                    self._res_lst = lst_res
                    self._res_issues_lst = lst_res_issues

                    def _repop_res():
                        lst_res.delete(0, "end")
                        lst_res_issues.delete(0, "end")
                        try:
                            rep = build_resolution_validation_report(cpath)
                            for b, lab in rep.get("resolutions", {}).items():
                                lst_res.insert("end", f"{b}: {lab}")
                            for iss in rep.get("issues", []):
                                lst_res_issues.insert("end", iss)
                            sc = rep.get("sample_count", 0)
                            if sc:
                                lst_res.insert("end", f"(sample_count: {sc})")
                            if "error" in rep:
                                lst_res_issues.insert("end", f"ERR: {rep['error']}")
                        except Exception as ex:
                            lst_res_issues.insert("end", f"ERR: {ex}")

                    _repop_res()

                    def _refresh_res():
                        _repop_res()
                        self.status_var.set("Resolution validation refreshed (view-only, no writes).")

                    ttk.Button(f, text="Refresh Resolution Validation", command=_refresh_res).pack(pady=2)
                    ttk.Label(f, text="View-only in Phase 3d. Shows resolution labels from library scan + naming style. No editing, no config/media writes. Resolution editing is deferred.").pack(pady=2)
                else:
                    # characters view (updated note)
                    lst = tk.Listbox(f, height=12)
                    lst.pack(fill="both", expand=True, padx=4, pady=2)
                    src = (self.correction_known or {}).get(cat, [])
                    for v in src:
                        lst.insert("end", v)
                    if cat == "characters":
                        note = "View-only in Phase 3d. Do not edit character_mappings/canonical_character_aliases here (use dedicated tabs). Learned editable in its tab. Destination folders now have dedicated view+validation tab (Phase 3d)."
                    else:
                        note = "View-only in Phase 3d. Do not edit destination folders or resolutions (dedicated view+validation tabs above; no editing in 3d). Use JSON directly with backups for advanced changes."
                    ttk.Label(f, text=note).pack(pady=2)

        def _save_known_values_changes():
            # Phase 3d save (extended from 3c/3b/3a): use pures for 4 config sections + learned (separate file).
            # Backups (collision-proof %f) created inside helpers before any write.
            # Only the 5 allowed (3a/3b/3c) keys + the learned file are mutated; r34_config.json other keys + char_mappings/canon_* untouched; dest/res views (3d) never edited or written.
            try:
                cpath = Path(self.config_var.get().strip() or str(DEFAULT_CONFIG))
                if not cpath or not cpath.exists():
                    messagebox.showerror("Config Path", "No valid r34_config.json path.")
                    return

                aa = getattr(self, "_edit_artist_aliases", {}) or {}
                fa = getattr(self, "_edit_folder_aliases", {}) or {}
                cm = getattr(self, "_edit_character_mappings", {}) or {}
                cca = getattr(self, "_edit_canonical_character_aliases", {}) or {}

                backup = apply_known_values_edits_to_config(
                    cpath,
                    artist_aliases=aa,
                    folder_aliases=fa,
                    character_mappings=cm,
                    canonical_character_aliases=cca,
                )
                if not backup:
                    messagebox.showerror("Save Error", "Config backup failed or invalid path; no write performed.")
                    return

                # Phase 3c learned (separate file, resolved rel to this cpath)
                lm = getattr(self, "_edit_learned_mappings", {}) or {}
                learned_p = resolve_learned_mappings_path(cpath, cfg)
                learned_backup = apply_learned_mappings_edits(learned_p, lm)
                # learned_backup may be None if file did not exist (created; documented in msg)

                # Refresh live in open correction tool dropdowns (known values + picks cbs; learned now included)
                self.correction_known = get_known_values(cpath)
                if hasattr(self, "_refresh_known_lists"):
                    self._refresh_known_lists()

                learned_msg = ""
                if learned_backup:
                    learned_msg = f"Learned mappings backup: {learned_backup.name}\n"
                elif lm or True:
                    learned_msg = f"Learned mappings file: {learned_p.name} (created/updated; no prior backup as file did not exist before this save)\n"

                messagebox.showinfo(
                    "Phase 3c Saved",
                    f"Config updated (artist_aliases, folder_aliases, character_mappings, canonical_character_aliases only).\n"
                    f"Backup created: {backup.name}\n"
                    f"{learned_msg}"
                    "All other config structure/keys + learned file safety preserved (r34_config.json untouched outside the 4; no bleed to char_mappings/canon_*; dest/res never edited).\n"
                    "Dropdowns refreshed (learned appears in Characters/Franchises where applicable). Current Correction Tool session remains usable."
                )
            except Exception as e:
                messagebox.showerror("Phase 3c Save Error", str(e))

        btns = ttk.Frame(win)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Save Changes (create collision-proof timestamped backups; 5 editable sections + learned in Phase 3c; dest/res 3d views untouched)", command=_save_known_values_changes).pack(side="left", padx=6)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right", padx=6)

    def _sort_tree(self, col):
        """Sort the underlying rows and refresh the tree view."""
        if not hasattr(self, "_sort_state"):
            self._sort_state = {}
        reverse = self._sort_state.get(col, False)

        key_funcs = {
            "status": lambda r: (r.get("status", "") or "").lower(),
            "current_target": lambda r: ((r.get("target_folder", "") or "") + "/" + (r.get("target_filename", "") or "")).lower(),
            "character": lambda r: (r.get("character", "") or "").lower(),
            "original": lambda r: (r.get("original_name", "") or "").lower(),
        }
        if col not in key_funcs:
            return

        self.correction_rows.sort(key=key_funcs[col], reverse=reverse)
        self._sort_state[col] = not reverse
        self._refresh_correction_tree()

    def _refresh_correction_tree(self):
        """Re-populate the tree from the current (possibly sorted) order of correction_rows."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        tree.delete(*tree.get_children())
        for i, row in enumerate(self.correction_rows):
            checked = "☑" if row.get("_bulk_checked") else "☐"
            orig = row.get("original_name", "")
            artist = row.get("artist", "")
            char = row.get("character", "")
            folder = row.get("target_folder", "")
            fname = row.get("target_filename", "")
            current_target = f"{folder}/{fname}" if folder and fname else (fname or "")
            status = row.get("status", "")
            approved = row.get("approved", "")
            tree.insert("", "end", iid=str(i), values=(checked, orig, artist, char, current_target, status, approved))

    def _reset_selected_row(self):
        # This is a simple version — for full reset we'd need the original CSV backup.
        self.status_var.set("Reset: close the Correction Tool without saving and re-open the original CSV to discard edits.")

    def _save_corrections(self):
        """Write the corrected rows back to the CSV."""
        if not hasattr(self, "correction_rows") or not hasattr(self, "correction_plan_path"):
            messagebox.showerror("Error", "No corrections loaded.")
            return

        try:
            # Ensure filenames remain unique after manual edits
            if hasattr(org, "deduplicate_target_filenames"):
                org.deduplicate_target_filenames(self.correction_rows)

            org.write_csv(Path(self.correction_plan_path), self.correction_rows)
            self.append_output(f"Corrections saved to: {self.correction_plan_path}")
            self.status_var.set("Corrections saved to CSV. Ready for Apply Approved Plan.")
            self.correction_window.destroy()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save corrections: {e}")

    def _print_csv_results_to_console(self, csv_path: Path, max_detail_rows: int = 100):
        """Read the generated preview CSV and print the results in a readable format
        directly to the GUI Console Output (in addition to the file being written).
        """
        try:
            import csv as _csv

            with csv_path.open(newline="", encoding="utf-8-sig") as f:
                reader = _csv.DictReader(f)
                rows = list(reader)

            if not rows:
                self.append_output("No rows found in preview CSV.")
                return

            self.append_output("\n" + "=" * 70)
            self.append_output(f"PREVIEW RESULTS ({len(rows)} files)")
            self.append_output("=" * 70)

            # Summary by status
            status_counts: dict[str, int] = {}
            for r in rows:
                status = r.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            self.append_output("Status summary:")
            for status, count in sorted(status_counts.items()):
                self.append_output(f"  {status}: {count}")
            self.append_output("")

            # === Block 1: Original Files ===
            self.append_output("=== Original Files ===")
            detail_rows = rows[:max_detail_rows]
            for i, row in enumerate(detail_rows, 1):
                orig = row.get("original_name", "")
                self.append_output(f"  {i:3}. {orig}")

            if len(rows) > max_detail_rows:
                self.append_output(f"  ... ({len(rows) - max_detail_rows} more files omitted from console)")

            self.append_output("")

            # === Block 2: Revised Names ===
            self.append_output("=== Revised Names ===")
            for i, row in enumerate(detail_rows, 1):
                orig = row.get("original_name", "")
                folder = row.get("target_folder", "").strip()
                fname = row.get("target_filename", "").strip()
                status = row.get("status", "")

                if fname:
                    # Show ONLY the actual filename that will be created on disk.
                    # Do not prepend the folder — the user wants the log to match
                    # exactly what they will see in Windows Explorer.
                    revised = fname
                else:
                    revised = "(no target generated - needs review)"

                self.append_output(f"  {i:3}. {revised}")

            if len(rows) > max_detail_rows:
                self.append_output(f"  ... ({len(rows) - max_detail_rows} more files omitted from console)")

            self.append_output(f"\nFull details are in: {csv_path}")
            self.append_output("=" * 70 + "\n")

        except Exception as e:
            self.append_output(f"Failed to print preview results to console: {e}", tag="error")

    def open_output_folder(self):
        # Try to open the most recent preview folder or source
        source = self.source_var.get().strip()
        if source and Path(source).exists():
            os.startfile(source)
            return

        # Fallback to script dir
        os.startfile(str(SCRIPT_DIR))

    def after_preview_completed(self, return_code: int, source: str):
        """Called from the completion handler (future enhancement)."""
        pass


def main():
    root = tk.Tk()
    app = OrganizerGUI(root)

    # Center window
    root.update_idletasks()
    w = root.winfo_width()
    h = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (w // 2)
    y = (root.winfo_screenheight() // 2) - (h // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.mainloop()


if __name__ == "__main__":
    main()
