"""
gui.py — Desktop launcher for SUMAC BOT.

Replaces the Flask web UI.  Run with:  python gui.py
"""

import datetime
import io
import sys
import threading
import time
from pathlib import Path

import customtkinter as ctk

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# When frozen by PyInstaller, write persistent files next to the .exe.
# __file__ points to a temp folder that is wiped on exit, so it cannot
# be used for files the user needs to persist across runs.
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

EMAIL_CONFIG    = SCRIPT_DIR / "email.txt"
DROPBOX_CONFIG  = SCRIPT_DIR / "config.txt"
SCHEDULE_CONFIG = SCRIPT_DIR / "schedule.txt"
SCHEDULE_SLOTS  = 6  # number of auto-start time slots in the Scheduler tab

# Default CTk blue — used to restore the Start button after a run.
_CTK_BLUE       = ("#3B8ED0", "#1F6AA5")
_CTK_BLUE_HOVER = ("#36719F", "#144870")


class _LogRedirect(io.TextIOBase):
    """Forwards print() / stderr output from the worker thread to the GUI log.

    Uses tkinter's after() so all GUI writes happen on the main thread, which
    is required by Tk.  Works whether running as a plain script or a frozen
    PyInstaller executable (no subprocess needed).
    """
    def __init__(self, after_fn, log_fn):
        self._after = after_fn
        self._log   = log_fn

    def write(self, text):
        if text:
            self._after(0, self._log, text)
        return len(text)

    def flush(self):
        pass


class SumacBotGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("SUMAC BOT")
        self.geometry("640x600")
        self.resizable(False, False)

        self._bot_running = False
        self._stop_requested = False

        self.tabview = ctk.CTkTabview(self, width=620)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)

        self.tabview.add("Bot")
        self.tabview.add("Settings")
        self.tabview.add("Scheduler")

        self._build_bot_tab()
        self._build_settings_tab()
        self._build_scheduler_tab()

        threading.Thread(target=self._scheduler_thread, daemon=True).start()

    # ── Bot tab ───────────────────────────────────────────────────────────────

    def _build_bot_tab(self):
        tab = self.tabview.tab("Bot")

        ctk.CTkLabel(
            tab, text="SUMAC BOT",
            font=ctk.CTkFont(size=34, weight="bold"),
        ).pack(pady=(20, 4))

        self.status_label = ctk.CTkLabel(
            tab, text="Ready",
            font=ctk.CTkFont(size=14),
            text_color="gray",
        )
        self.status_label.pack(pady=(0, 14))

        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.pack(pady=(0, 12))

        self.start_btn = ctk.CTkButton(
            btn_frame, text="▶   Start",
            width=170, height=52,
            font=ctk.CTkFont(size=17, weight="bold"),
            command=self._start,
        )
        self.start_btn.pack(side="left", padx=14)

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="■   Stop",
            width=170, height=52,
            font=ctk.CTkFont(size=17, weight="bold"),
            fg_color="#C0392B", hover_color="#922B21",
            command=self._stop,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=14)

        self.log_box = ctk.CTkTextbox(
            tab, width=580, height=310,
            font=ctk.CTkFont(family="Courier New", size=12),
            state="disabled",
        )
        self.log_box.pack(padx=10, pady=(0, 10))

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self):
        tab = self.tabview.tab("Settings")

        ctk.CTkLabel(
            tab, text="Email Notification Recipients",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(24, 12))

        self._recipients_frame = ctk.CTkScrollableFrame(tab, width=540, height=150)
        self._recipients_frame.pack(padx=16, pady=(0, 12))

        add_frame = ctk.CTkFrame(tab, fg_color="transparent")
        add_frame.pack(pady=(0, 8))

        self._new_email_entry = ctk.CTkEntry(
            add_frame, width=380,
            placeholder_text="Enter email address…",
            font=ctk.CTkFont(size=14),
        )
        self._new_email_entry.pack(side="left", padx=(0, 10))
        self._new_email_entry.bind("<Return>", lambda _: self._add_recipient())

        ctk.CTkButton(
            add_frame, text="Add", width=110, height=36,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._add_recipient,
        ).pack(side="left")

        self._refresh_recipient_list()

        ctk.CTkLabel(
            tab, text="Dropbox Destination Folder",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(32, 8))

        dropbox_frame = ctk.CTkFrame(tab, fg_color="transparent")
        dropbox_frame.pack(pady=(0, 8))

        self._dropbox_entry = ctk.CTkEntry(
            dropbox_frame, width=380,
            font=ctk.CTkFont(size=13),
        )
        self._dropbox_entry.insert(0, self._read_dropbox_path())
        self._dropbox_entry.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            dropbox_frame, text="Save", width=110, height=36,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._save_dropbox_path,
        ).pack(side="left")

    def _read_recipients(self) -> list[str]:
        if not EMAIL_CONFIG.exists():
            return []
        with open(EMAIL_CONFIG, encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines()]
        return [l for l in lines[2:] if l]

    def _write_recipients(self, recipients: list[str]) -> None:
        if not EMAIL_CONFIG.exists():
            return
        with open(EMAIL_CONFIG, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f.readlines()]
        new_content = "\n".join(lines[:2] + recipients) + "\n"
        with open(EMAIL_CONFIG, "w", encoding="utf-8") as f:
            f.write(new_content)

    def _refresh_recipient_list(self) -> None:
        for widget in self._recipients_frame.winfo_children():
            widget.destroy()
        for email in self._read_recipients():
            self._add_recipient_row(email)

    def _add_recipient_row(self, email: str) -> None:
        row = ctk.CTkFrame(self._recipients_frame, fg_color=("gray85", "gray20"))
        row.pack(fill="x", pady=3, padx=4)

        ctk.CTkLabel(
            row, text=email,
            font=ctk.CTkFont(size=13),
            anchor="w",
        ).pack(side="left", padx=10, fill="x", expand=True)

        def _remove(e=email, r=row):
            r.destroy()
            self._write_recipients([x for x in self._read_recipients() if x != e])

        ctk.CTkButton(
            row, text="Remove", width=84, height=28,
            font=ctk.CTkFont(size=12),
            fg_color="#C0392B", hover_color="#922B21",
            command=_remove,
        ).pack(side="right", padx=6, pady=4)

    def _add_recipient(self) -> None:
        email = self._new_email_entry.get().strip()
        if not email or "@" not in email:
            return
        current = self._read_recipients()
        if email in current:
            self._new_email_entry.delete(0, "end")
            return
        self._write_recipients(current + [email])
        self._add_recipient_row(email)
        self._new_email_entry.delete(0, "end")

    def _read_dropbox_path(self) -> str:
        if DROPBOX_CONFIG.exists():
            path = DROPBOX_CONFIG.read_text(encoding="utf-8").strip()
            if path:
                return path
        return r"C:\Users\luisd\Dropbox\Coquibot"

    def _save_dropbox_path(self) -> None:
        path = self._dropbox_entry.get().strip()
        if path:
            DROPBOX_CONFIG.write_text(path, encoding="utf-8")

    # ── Bot tab handlers ──────────────────────────────────────────────────────

    def _start(self):
        if self._bot_running:
            return
        self._bot_running = True
        self._stop_requested = False
        self.start_btn.configure(
            text="⏳   Running…",
            fg_color="#CA6F1E", hover_color="#CA6F1E",
            state="disabled",
        )
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Running…", text_color="#E67E22")
        self._log_clear()
        self._log("Starting SUMAC Bot…\n\n")

        threading.Thread(target=self._run_bot, daemon=True).start()

    def _stop(self):
        self._log("\n[Stopping…]\n")
        self._stop_requested = True
        self.stop_btn.configure(state="disabled")
        import sumac_login
        sumac_login.stop()
        # Do NOT reset to idle here — the background thread from _run_bot is
        # still winding down (its current Playwright call needs to raise once
        # the browser closes). Re-enabling Start now would let a second run()
        # start while the first is still writing PDFs, causing duplicate
        # downloads. _on_done() (called when the thread actually exits) is the
        # only place that re-enables Start.

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _run_bot(self):
        import sumac_login   # imported here so PyInstaller bundles it correctly

        redirector = _LogRedirect(self.after, self._log)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = redirector

        try:
            sumac_login.run()
        except Exception as e:
            self.after(0, self._log, f"\n[Error: {e}]\n")
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        self.after(0, self._on_done)

    def _on_done(self):
        status_text = "Stopped" if self._stop_requested else "Finished"
        self._log(f"\n[{status_text}]\n")
        self._set_idle(status_text)

    def _set_idle(self, status_text: str):
        self._bot_running = False
        self.start_btn.configure(
            text="▶   Start",
            fg_color=_CTK_BLUE,
            hover_color=_CTK_BLUE_HOVER,
            state="normal",
        )
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text=status_text, text_color="gray")

    def _log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_clear(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")


    # ── Scheduler tab ─────────────────────────────────────────────────────────

    def _build_scheduler_tab(self):
        tab = self.tabview.tab("Scheduler")

        ctk.CTkLabel(
            tab, text="Scheduled Auto-Start",
            font=ctk.CTkFont(size=17, weight="bold"),
        ).pack(pady=(12, 2))

        ctk.CTkLabel(
            tab, text="Bot starts automatically at the enabled times (24-hour HH:MM).",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        ).pack(pady=(0, 8))

        self._schedule_entries  = []
        self._schedule_switches = []

        for i in range(SCHEDULE_SLOTS):
            row = ctk.CTkFrame(tab, fg_color=("gray85", "gray20"), corner_radius=8)
            row.pack(fill="x", padx=40, pady=3)

            sw = ctk.CTkSwitch(
                row, text=f"  Slot {i + 1}",
                font=ctk.CTkFont(size=14),
                width=110,
            )
            sw.pack(side="left", padx=(16, 12), pady=8)
            self._schedule_switches.append(sw)

            entry = ctk.CTkEntry(
                row, width=90, placeholder_text="HH:MM",
                font=ctk.CTkFont(size=14),
            )
            entry.pack(side="left", padx=(0, 16), pady=8)
            self._schedule_entries.append(entry)

        ctk.CTkButton(
            tab, text="Save Schedule", width=180, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._save_schedule,
        ).pack(pady=(12, 6))

        self._scheduler_status = ctk.CTkLabel(
            tab, text="No active schedule.",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        self._scheduler_status.pack()

        self._load_schedule()

    def _load_schedule(self) -> None:
        if not SCHEDULE_CONFIG.exists():
            return
        lines = SCHEDULE_CONFIG.read_text(encoding="utf-8").strip().splitlines()
        for i, line in enumerate(lines[:SCHEDULE_SLOTS]):
            parts = line.split(",")
            if len(parts) >= 2:
                self._schedule_entries[i].delete(0, "end")
                self._schedule_entries[i].insert(0, parts[0].strip())
                if parts[1].strip() == "1":
                    self._schedule_switches[i].select()
                else:
                    self._schedule_switches[i].deselect()
        self._refresh_scheduler_status()

    def _save_schedule(self) -> None:
        lines = []
        for i in range(SCHEDULE_SLOTS):
            raw = self._schedule_entries[i].get().strip()
            try:
                datetime.datetime.strptime(raw, "%H:%M")
                time_str = raw
            except ValueError:
                time_str = ""
                self._schedule_entries[i].delete(0, "end")
            enabled = "1" if self._schedule_switches[i].get() else "0"
            lines.append(f"{time_str},{enabled}")
        SCHEDULE_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._refresh_scheduler_status()

    def _refresh_scheduler_status(self) -> None:
        active = []
        if SCHEDULE_CONFIG.exists():
            for line in SCHEDULE_CONFIG.read_text(encoding="utf-8").strip().splitlines()[:SCHEDULE_SLOTS]:
                parts = line.split(",")
                if len(parts) >= 2 and parts[1].strip() == "1" and parts[0].strip():
                    active.append(parts[0].strip())
        if active:
            self._scheduler_status.configure(
                text=f"Active slots: {', '.join(active)}",
                text_color="#2ECC71",
            )
        else:
            self._scheduler_status.configure(
                text="No active schedule.",
                text_color="gray",
            )

    def _scheduler_thread(self) -> None:
        triggered: set = set()
        last_date = None

        while True:
            now = datetime.datetime.now()
            today = now.date()
            hhmm  = now.strftime("%H:%M")

            if today != last_date:
                triggered.clear()
                last_date = today

            if SCHEDULE_CONFIG.exists():
                try:
                    lines = SCHEDULE_CONFIG.read_text(encoding="utf-8").strip().splitlines()
                    for i, line in enumerate(lines[:SCHEDULE_SLOTS]):
                        parts = line.split(",")
                        if len(parts) < 2:
                            continue
                        t_str   = parts[0].strip()
                        enabled = parts[1].strip() == "1"
                        key     = (today, i, t_str)
                        if enabled and t_str == hhmm and key not in triggered:
                            triggered.add(key)
                            self.after(0, self._auto_start, i)
                except Exception:
                    pass

            time.sleep(20)

    def _auto_start(self, slot_idx: int = 0) -> None:
        if not self._bot_running:
            self.tabview.set("Bot")
            self._log(f"[Scheduler] Auto-start triggered (slot {slot_idx + 1}).\n")
            self._start()


if __name__ == "__main__":
    app = SumacBotGUI()
    app.mainloop()
