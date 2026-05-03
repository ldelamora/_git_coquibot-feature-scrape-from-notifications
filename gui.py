"""
gui.py — Desktop launcher for SUMAC BOT.

Replaces the Flask web UI.  Run with:  python gui.py
"""

import subprocess
import sys
import threading
from pathlib import Path

import customtkinter as ctk

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

SCRIPT_DIR = Path(__file__).parent

# Default CTk blue — used to restore the Start button after a run.
_CTK_BLUE        = ("#3B8ED0", "#1F6AA5")
_CTK_BLUE_HOVER  = ("#36719F", "#144870")


class SumacBotGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("SUMAC BOT")
        self.geometry("620x540")
        self.resizable(False, False)

        self._process: subprocess.Popen | None = None

        # ── Header ────────────────────────────────────────────────────────────
        ctk.CTkLabel(
            self, text="SUMAC BOT",
            font=ctk.CTkFont(size=34, weight="bold"),
        ).pack(pady=(30, 4))

        self.status_label = ctk.CTkLabel(
            self, text="Ready",
            font=ctk.CTkFont(size=14),
            text_color="gray",
        )
        self.status_label.pack(pady=(0, 18))

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=(0, 16))

        self.start_btn = ctk.CTkButton(
            btn_frame,
            text="▶   Start",
            width=170, height=52,
            font=ctk.CTkFont(size=17, weight="bold"),
            command=self._start,
        )
        self.start_btn.pack(side="left", padx=14)

        self.stop_btn = ctk.CTkButton(
            btn_frame,
            text="■   Stop",
            width=170, height=52,
            font=ctk.CTkFont(size=17, weight="bold"),
            fg_color="#C0392B", hover_color="#922B21",
            command=self._stop,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=14)

        # ── Log area ──────────────────────────────────────────────────────────
        self.log_box = ctk.CTkTextbox(
            self,
            width=580, height=300,
            font=ctk.CTkFont(family="Courier New", size=12),
            state="disabled",
        )
        self.log_box.pack(padx=20, pady=(0, 20))

    # ── Button handlers ───────────────────────────────────────────────────────

    def _start(self):
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
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self._log("\n[Stopped by user]\n")
        self._set_idle("Stopped")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _run_bot(self):
        self._process = subprocess.Popen(
            [sys.executable, "-c", "import sumac_login; sumac_login.run()"],
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in self._process.stdout:
            self.after(0, self._log, line)
        self._process.wait()
        self.after(0, self._on_done)

    def _on_done(self):
        self._log("\n[Finished]\n")
        self._set_idle("Finished")

    def _set_idle(self, status_text: str):
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


if __name__ == "__main__":
    app = SumacBotGUI()
    app.mainloop()
