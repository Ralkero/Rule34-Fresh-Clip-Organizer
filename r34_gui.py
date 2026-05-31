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

        columns = ("original", "artist", "character", "current_target", "status")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("original", text="Original Filename")
        tree.heading("artist", text="Artist")
        tree.heading("character", text="Character")
        tree.heading("current_target", text="Current Target")
        tree.heading("status", text="Status")

        tree.column("original", width=320)
        tree.column("artist", width=140)
        tree.column("character", width=160)
        tree.column("current_target", width=280)
        tree.column("status", width=100)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Populate tree
        for i, row in enumerate(rows):
            orig = row.get("original_name", "")
            artist = row.get("artist", "")
            char = row.get("character", "")
            folder = row.get("target_folder", "")
            fname = row.get("target_filename", "")
            current_target = f"{folder}/{fname}" if folder and fname else (fname or "")
            status = row.get("status", "")
            tree.insert("", "end", iid=str(i), values=(orig, artist, char, current_target, status))

        self.correction_tree = tree
        self.correction_rows = rows

        # Edit panel
        edit_frame = ttk.LabelFrame(win, text="Correct Selected Row", padding=10)
        edit_frame.pack(fill="x", padx=10, pady=8)

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

        btn_apply_corr = ttk.Button(btn_frame, text="Apply Correction to Selected Row", command=self._apply_correction)
        btn_apply_corr.pack(side="left", padx=5)
        Tooltip(btn_apply_corr, "Applies the three values in the edit fields (Corrected Target Folder / Target Filename / Notes) to the row currently selected in the table above. The change is performed in memory only. It also appends a timestamped 'manual_correction: YYYY-MM-DD HH:MM' entry (plus your Notes text) to the row's notes and reason columns so the audit trail is complete. The full target_path is recomputed using your current Dest root + config. Click Save All Changes afterward to persist everything to disk.")

        btn_mark = ttk.Button(btn_frame, text="Mark as Approved", command=self._mark_selected_approved)
        btn_mark.pack(side="left", padx=5)
        Tooltip(btn_mark, "Sets approved='yes' on the currently selected row (identical to editing the CSV by hand). After marking, the row will be included when you run Apply Approved Plan from the main window. Use this after you have reviewed or manually corrected a difficult filename so it is no longer skipped.")

        btn_reset = ttk.Button(btn_frame, text="Reset Selected Row", command=self._reset_selected_row)
        btn_reset.pack(side="left", padx=5)
        Tooltip(btn_reset, "Placeholder action (currently shows an info dialog). A future enhancement could reload the original values for the selected row from a hidden backup copy of the preview CSV. For now, simply close this Correction Tool window without saving and re-open it from the original preview CSV if you want to discard all edits.")

        # Bind selection
        tree.bind("<<TreeviewSelect>>", self._on_correction_row_selected)

        # Bottom bar
        bottom = ttk.Frame(win, padding=8)
        bottom.pack(fill="x")
        btn_save = ttk.Button(bottom, text="Save All Changes to CSV", command=self._save_corrections)
        btn_save.pack(side="right")
        Tooltip(btn_save, "Writes the entire in-memory table (including every manual correction, approval change, and note you made) back to the original preview CSV file on disk using the same format the organizer expects. After a successful save the Correction window closes automatically. You can then immediately click 'Apply Approved Plan' in the main window — all your edits will be honored and any new character→folder pairs from corrected rows will be learned on success. The audit tags you added are preserved in the CSV so they appear in the final apply log.")
        ttk.Label(bottom, text="Corrections will be visible in the reason/notes when you Apply.").pack(side="left")

        self.correction_window = win

    def _on_correction_row_selected(self, event=None):
        """Load selected row into the edit fields."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            return
        iid = selection[0]
        idx = int(iid)
        row = self.correction_rows[idx]

        self.corr_folder_var.set(row.get("target_folder", ""))
        self.corr_filename_var.set(row.get("target_filename", ""))
        self.corr_notes_var.set(row.get("notes", ""))

    def _apply_correction(self):
        """Apply manual correction from the edit fields to the selected row."""
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a row in the list first.")
            return

        iid = selection[0]
        idx = int(iid)
        row = self.correction_rows[idx]

        new_folder = self.corr_folder_var.get().strip()
        new_filename = self.corr_filename_var.get().strip()
        notes = self.corr_notes_var.get().strip()

        if not new_filename:
            messagebox.showerror("Error", "Corrected Target Filename cannot be empty.")
            return

        # Update the row
        if new_folder:
            row["target_folder"] = new_folder
        row["target_filename"] = new_filename

        # Recompute target_path using current dest root
        dest_root = self.dest_var.get().strip() or ""
        if not dest_root:
            # Try to load from config
            try:
                cfg = org.load_config(Path(self.config_var.get() or DEFAULT_CONFIG))
                dest_root = str(cfg.destination_root)
            except Exception:
                dest_root = ""

        if dest_root and new_folder and new_filename:
            target_path = str(Path(dest_root) / new_folder / new_filename)
            row["target_path"] = target_path

        # Add correction note
        existing_notes = row.get("notes", "")
        correction_note = f"manual_correction: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if notes:
            correction_note += f" - {notes}"
        row["notes"] = f"{existing_notes}; {correction_note}".strip("; ")

        # Update reason to make it auditable
        existing_reason = row.get("reason", "")
        row["reason"] = f"{existing_reason};manual_filename_correction".strip(";")

        # Refresh tree display
        tree = self.correction_tree
        current_target = f"{row.get('target_folder','')}/{new_filename}" if row.get("target_folder") else new_filename
        tree.item(iid, values=(
            row.get("original_name", ""),
            row.get("artist", ""),
            row.get("character", ""),
            current_target,
            row.get("status", "")
        ))

        self.append_output(f"Applied manual correction to: {row.get('original_name')}")

    def _mark_selected_approved(self):
        tree = getattr(self, "correction_tree", None)
        if not tree:
            return
        selection = tree.selection()
        if not selection:
            return
        iid = selection[0]
        idx = int(iid)
        row = self.correction_rows[idx]
        row["approved"] = "yes"
        self.append_output(f"Marked as approved: {row.get('original_name')}")

    def _reset_selected_row(self):
        # This is a simple version — for full reset we'd need the original CSV backup.
        messagebox.showinfo("Info", "Reset functionality can be added by keeping a backup of the original preview CSV.")

    def _save_corrections(self):
        """Write the corrected rows back to the CSV."""
        if not hasattr(self, "correction_rows") or not hasattr(self, "correction_plan_path"):
            messagebox.showerror("Error", "No corrections loaded.")
            return

        try:
            org.write_csv(Path(self.correction_plan_path), self.correction_rows)
            self.append_output(f"Corrections saved to: {self.correction_plan_path}")
            messagebox.showinfo("Saved", "Corrections have been written to the CSV.\nYou can now run Apply.")
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
                    if folder:
                        # Use " - " separator in console display to match the user's
                        # preferred naming style (e.g. "King of Fighters - Mai [1080P].mp4")
                        revised = f"{folder} - {fname}" if not fname.startswith(folder) else fname
                    else:
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
