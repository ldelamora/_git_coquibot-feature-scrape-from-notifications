"""
SearchCasesDocumentsApp.py — SUMAC case document search & download tool.

Enter one or more case numbers (comma-separated), click Start.
For each case the app:
  1. Logs into SUMAC (credentials from sumac.txt).
  2. Opens the BÚSQUEDA search panel.
  3. Searches for the case number and clicks the first result.
  4. Loops through every expediente in that case.
  5. Downloads only the Documento PDF for each expediente.
  6. Saves PDFs to SearchCasesDownloads/ (created next to this script).
  7. Moves on to the next case number and repeats.

No email notifications, no Dropbox transfer.
Run with:  python SearchCasesDocumentsApp.py
"""

import base64
import io
import os
import re
import sys
import time
import threading
from pathlib import Path

# When frozen by PyInstaller the Playwright driver looks for the browser
# inside the temp extraction folder, which doesn't contain it.
# Set PLAYWRIGHT_BROWSERS_PATH before importing playwright so it points
# to the ms-playwright folder sitting next to the exe.
if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).parent
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(_exe_dir / "ms-playwright")
    )

import customtkinter as ctk
from playwright.sync_api import sync_playwright

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

SUMAC_URL        = "https://tribunalelectronico.ramajudicial.pr/sumac2018/signIn.html"
CREDENTIALS_FILE = SCRIPT_DIR / "sumac.txt"
OUTPUT_DIR       = SCRIPT_DIR / "SearchCasesDownloads"

_CTK_BLUE       = ("#3B8ED0", "#1F6AA5")
_CTK_BLUE_HOVER = ("#36719F", "#144870")

_active_browser = None

MESES = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
    "ene": "01", "feb": "02", "mar": "03", "abr": "04",
    "may": "05", "jun": "06", "jul": "07", "ago": "08",
    "sep": "09", "oct": "10", "nov": "11", "dic": "12",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_credentials():
    with open(CREDENTIALS_FILE, "r") as f:
        lines = [l.strip() for l in f.readlines()]
    return lines[0], lines[1]


def _stop_browser():
    global _active_browser
    if _active_browser:
        try:
            _active_browser.close()
        except Exception:
            pass


def _parse_date(tile) -> str:
    """Extract yyyy-mm-dd from a .caseEntryTile__simpleView tile's date block."""
    try:
        day = tile.locator(".dateBlock__day").first.inner_text(timeout=1000).strip()
        mon = tile.locator(".dateBlock__month").first.inner_text(timeout=1000).strip().lower()
        yr2 = tile.locator(".dateBlock__year").first.inner_text(timeout=1000).strip().lstrip("-")
        month = MESES.get(mon, "")
        if month and yr2:
            return f"20{yr2}-{month}-{day.zfill(2)}"
    except Exception:
        pass
    return ""


def _sanitize(text: str, max_len: int = 50) -> str:
    text = re.sub(r'[\\/:*?"<>|.]+', "", text).strip()
    return re.sub(r"\s+", " ", text)[:max_len]


def _already_downloaded(filename_prefix: str) -> bool:
    if not OUTPUT_DIR.exists():
        return False
    return any(
        f.name.startswith(filename_prefix)
        and len(f.name) > len(filename_prefix)
        and f.name[len(filename_prefix)] in (".", " ")
        for f in OUTPUT_DIR.iterdir() if f.is_file()
    )


def _save_blob(page, url: str, save_path: str) -> bool:
    """Fetch a blob: URL from inside the browser and save it to disk."""
    if not url.startswith("blob:"):
        return False
    try:
        b64 = page.evaluate("""async (u) => {
            const r = await fetch(u);
            const b = await r.arrayBuffer();
            const a = new Uint8Array(b);
            let s = '';
            for (let i = 0; i < a.byteLength; i++) s += String.fromCharCode(a[i]);
            return btoa(s);
        }""", url)
        data = base64.b64decode(b64)
        if data[:4] != b"%PDF":
            return False
        with open(save_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"    Blob fetch failed: {e}")
        return False


def _download_documento(page, filename_prefix: str,
                        stale_blob_srcs: set,
                        captured_pdf_urls: list,
                        captured_pdf_data: dict) -> bool:
    """
    Download the Documento PDF for the currently open expediente detail view.

    Strategy order (mirrors sumac_login.py):
      0a. Left-pillbox download button (anejos active) → expect_download
      0b. Left-pillbox iframe blob URL (visible, not stale)
          → if left pillbox is present but both fail: text-only entry, skip
      Tab-click fallback (no anejos):
      1.  Download button (.caseEntriesView__downloadButton) → expect_download
      0c. Visible main-area iframe blob URL (not stale)
      3.  Network-captured HTTP URL (cleared per expediente)
    """
    OUTPUT_DIR.mkdir(exist_ok=True)

    if _already_downloaded(filename_prefix):
        print("    Already downloaded — skipping.")
        return True

    # ── Strategy 0: left pillbox (when anejos are active) ─────────────────────
    left_dl_btn = page.locator(
        ".caseEntryDocumentContainer__leftPillbox"
        " .caseEntryDocumentContainer__downloadButton"
    )
    if left_dl_btn.count() > 0:
        left_title = ""
        try:
            h1 = page.locator(
                ".caseEntryDocumentContainer__leftPillbox"
                " .caseEntryDocumentContainer__documentHeader h1"
            ).first
            left_title = _sanitize(
                h1.get_attribute("title") or h1.inner_text(timeout=1000) or ""
            )
        except Exception:
            pass
        title_part = f" - {left_title}" if left_title else ""
        fname      = f"{filename_prefix}{title_part}.pdf"
        save_path  = str(OUTPUT_DIR / fname)

        # 0a: download button
        try:
            with page.expect_download(timeout=5000) as dl_info:
                left_dl_btn.click()
            dl = dl_info.value
            dl.save_as(save_path)
            print(f"    Saved: {fname}")
            return True
        except Exception as e:
            print(f"    Left pillbox button failed: {e}")

        # 0b: visible iframe blob (not stale)
        try:
            left_iframe = page.locator(
                ".caseEntryDocumentContainer__leftPillbox iframe.PDFViewer__embedArea"
            )
            if left_iframe.count() > 0 and left_iframe.first.is_visible():
                left_url = left_iframe.first.get_attribute("src", timeout=2000)
                if left_url and left_url not in stale_blob_srcs:
                    if _save_blob(page, left_url, save_path):
                        print(f"    Saved from blob: {fname}")
                        return True
        except Exception:
            pass

        # Left pillbox present but no downloadable PDF (text-only ORDEN/ENTERADO)
        print("    Left pillbox present but no PDF — skipping.")
        return False

    # ── Tab-click fallback (no anejos) ────────────────────────────────────────
    doc_tab = page.locator("button[title='Documento']")
    if doc_tab.count() == 0:
        doc_tab = page.locator("button").filter(has_text="Documento")
    if doc_tab.count() == 0:
        print("    Documento tab not found.")
        return False

    urls_before = list(captured_pdf_urls)
    doc_tab.first.click()

    # Poll up to 15 s for a network PDF URL to arrive
    for i in range(75):
        page.wait_for_timeout(200)
        new = [u for u in captured_pdf_urls if u not in urls_before]
        if new and any(u in captured_pdf_data for u in new):
            break
        if not new and i >= 9:
            break
        if new and i >= 14:
            break

    doc_title = ""
    try:
        h1 = page.locator(".caseEntryDocumentContainer__documentHeader h1").first
        doc_title = _sanitize(
            h1.get_attribute("title") or h1.inner_text(timeout=1000) or ""
        )
    except Exception:
        pass
    title_part = f" - {doc_title}" if doc_title else ""
    fname      = f"{filename_prefix}{title_part}.pdf"
    save_path  = str(OUTPUT_DIR / fname)

    new_urls = [u for u in captured_pdf_urls if u not in urls_before]

    if not new_urls:
        # Strategy 1: download button
        dl_btn = page.locator(".caseEntriesView__downloadButton")
        for j in range(dl_btn.count()):
            try:
                with page.expect_download(timeout=5000) as dl_info:
                    dl_btn.nth(j).click()
                dl = dl_info.value
                dl.save_as(save_path)
                print(f"    Saved: {fname}")
                return True
            except Exception:
                pass

        # Strategy 0c: visible main-area iframe blob (not stale)
        try:
            tab_iframe = page.locator("iframe.PDFViewer__embedArea").first
            if tab_iframe.count() > 0 and tab_iframe.is_visible():
                blob_url = tab_iframe.get_attribute("src", timeout=2000)
                if blob_url and blob_url not in stale_blob_srcs:
                    if _save_blob(page, blob_url, save_path):
                        print(f"    Saved from blob: {fname}")
                        return True
        except Exception:
            pass

    # Strategy 3: network-captured HTTP URL
    for url in (new_urls if new_urls else list(reversed(captured_pdf_urls))):
        if url in captured_pdf_data:
            data = captured_pdf_data[url]
            if data[:4] == b"%PDF":
                with open(save_path, "wb") as f:
                    f.write(data)
                print(f"    Saved from network: {fname}")
                captured_pdf_urls[:] = [u for u in captured_pdf_urls if u != url]
                captured_pdf_data.pop(url, None)
                return True

    print("    No PDF found for Documento.")
    return False


# ── Log redirect ──────────────────────────────────────────────────────────────

class _LogRedirect(io.TextIOBase):
    def __init__(self, after_fn, log_fn):
        self._after = after_fn
        self._log   = log_fn

    def write(self, text):
        if text:
            self._after(0, self._log, text)
        return len(text)

    def flush(self):
        pass


# ── GUI ───────────────────────────────────────────────────────────────────────

class SearchCasesApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("SUMAC — Download Documentos from List of Cases")
        self.geometry("640x580")
        self.resizable(False, False)

        self._running = False
        self._build_ui()

    def _build_ui(self):
        ctk.CTkLabel(
            self, text="Download Documentos from List of Cases",
            font=ctk.CTkFont(size=26, weight="bold"),
        ).pack(pady=(20, 4))

        ctk.CTkLabel(
            self, text="Enter case numbers separated by commas",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        ).pack(pady=(0, 10))

        self.cases_entry = ctk.CTkTextbox(
            self, width=580, height=80,
            font=ctk.CTkFont(size=14),
        )
        self.cases_entry.pack(padx=20)
        self.cases_entry.insert("1.0", "e.g.  BY2026RF00080, SJ2024CV10442")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=14)

        self.start_btn = ctk.CTkButton(
            btn_frame, text="▶   Start",
            width=170, height=48,
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self._start,
        )
        self.start_btn.pack(side="left", padx=12)

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="■   Stop",
            width=170, height=48,
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="#C0392B", hover_color="#922B21",
            command=self._stop,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=12)

        self.status_label = ctk.CTkLabel(
            self, text="Ready",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        )
        self.status_label.pack(pady=(0, 6))

        self.log_box = ctk.CTkTextbox(
            self, width=580, height=300,
            font=ctk.CTkFont(family="Courier New", size=12),
            state="disabled",
        )
        self.log_box.pack(padx=20, pady=(0, 16))

    # ── Button handlers ───────────────────────────────────────────────────────

    def _start(self):
        raw = self.cases_entry.get("1.0", "end").strip()
        case_numbers = [c.strip() for c in raw.split(",") if c.strip()]
        if not case_numbers:
            self._log("No case numbers entered.\n")
            return

        self._running = True
        self.start_btn.configure(
            text="⏳   Running…",
            fg_color="#CA6F1E", hover_color="#CA6F1E",
            state="disabled",
        )
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Running…", text_color="#E67E22")
        self._log_clear()
        self._log(f"Cases to process: {', '.join(case_numbers)}\n\n")

        threading.Thread(target=self._run, args=(case_numbers,), daemon=True).start()

    def _stop(self):
        self._log("\n[Stopping…]\n")
        _stop_browser()
        self._set_idle("Stopped")

    # ── Main automation ───────────────────────────────────────────────────────

    def _run(self, case_numbers: list[str]):
        global _active_browser

        redirector = _LogRedirect(self.after, self._log)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = redirector

        try:
            username, password = _read_credentials()
        except Exception as e:
            print(f"Could not read {CREDENTIALS_FILE}: {e}")
            sys.stdout, sys.stderr = old_out, old_err
            self.after(0, self._set_idle, "Credential error")
            return

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                _active_browser = browser
                page = browser.new_page()

                # ── Login ─────────────────────────────────────────────────
                print("Opening SUMAC…")
                page.goto(SUMAC_URL)
                page.fill(
                    "input[name='username'], input[type='text'], #username",
                    username,
                )
                page.fill(
                    "input[name='password'], input[type='password'], #password",
                    password,
                )
                print("Clicking Acceder…")
                login_btn = page.get_by_role("button", name="Acceder", exact=True)
                login_btn.wait_for(state="visible", timeout=5000)
                login_btn.click()

                print("Waiting for home page to load…")
                time.sleep(20)
                page.wait_for_load_state("networkidle")
                landing_url = page.url
                print(f"Logged in. URL: {landing_url}\n")

                # ── Shared network-capture buffers (cleared per expediente) ──
                captured_pdf_urls: list = []
                captured_pdf_data: dict = {}

                def on_response(response):
                    if response.status != 200:
                        return
                    ct  = response.headers.get("content-type", "")
                    url = response.url.lower()
                    if url.startswith("chrome-extension://"):
                        return
                    if "pdf" in ct.lower() or url.endswith(".pdf") or "pdf" in url:
                        captured_pdf_urls.append(response.url)
                        try:
                            data = response.body()
                            if data and data[:4] == b"%PDF":
                                captured_pdf_data[response.url] = data
                        except Exception:
                            pass

                page.on("response", on_response)

                # ── Process each case ─────────────────────────────────────
                for case_idx, case_number in enumerate(case_numbers):
                    print(f"\n{'='*55}")
                    print(f"Case: {case_number}")
                    print(f"{'='*55}")

                    # Open BÚSQUEDA
                    try:
                        busqueda_btn = page.locator(
                            "div[title='Búsqueda de casos y recursos']"
                        )
                        busqueda_btn.wait_for(state="visible", timeout=10000)
                        busqueda_btn.click()

                        # From the second case onwards the panel reopens showing
                        # the previous results — click the left-arrow button to
                        # return to the blank search form first.
                        # Skip this on the first case: the form is already clean
                        # and the 3-second timeout was causing the search to hang.
                        if case_idx > 0:
                            back_to_form = page.locator("button.searchView__showSearchButton")
                            try:
                                back_to_form.wait_for(state="visible", timeout=3000)
                                print("  Results panel open — clicking back to search form…")
                                back_to_form.click()
                            except Exception:
                                pass  # search form already showing, nothing to do

                        page.wait_for_selector(
                            "input[title='Número de Caso']", timeout=10000
                        )
                        page.locator("input[title='Número de Caso']").fill(case_number)
                        page.wait_for_timeout(3000)

                        buscar_btn = page.locator("button.searchView__footerButton")
                        buscar_btn.wait_for(state="visible", timeout=5000)
                        buscar_btn.click()

                        # Wait for results
                        first_result = page.locator(".caseTile__view").first
                        first_result.wait_for(state="visible", timeout=15000)
                        found_num = first_result.locator(
                            ".caseTile__caseNumber"
                        ).inner_text(timeout=2000).strip()
                        print(f"First result: {found_num} — opening…")
                        first_result.click()
                    except Exception as e:
                        print(f"Search/open failed for {case_number}: {e}")
                        page.goto(landing_url)
                        page.wait_for_timeout(3000)
                        continue

                    # Wait for expediente list
                    page.wait_for_timeout(4000)
                    try:
                        page.wait_for_selector(
                            ".caseEntryTile__simpleView", timeout=8000
                        )
                    except Exception:
                        print(f"No expediente tiles found for {case_number}.")
                        page.goto(landing_url)
                        page.wait_for_timeout(3000)
                        continue

                    # Snapshot expediente list before any navigation
                    exp_tiles   = page.locator(".caseEntryTile__simpleView")
                    exp_count   = exp_tiles.count()
                    exp_numbers = []
                    exp_dates   = []
                    for i in range(exp_count):
                        try:
                            num = exp_tiles.nth(i).locator(
                                ".caseEntryTile__number"
                            ).inner_text(timeout=2000).strip()
                        except Exception:
                            num = str(i + 1)
                        exp_numbers.append(num)
                        exp_dates.append(_parse_date(exp_tiles.nth(i)))

                    print(f"Found {exp_count} expedientes: {exp_numbers}")

                    # ── Process each expediente ────────────────────────────
                    for i, exp_number in enumerate(exp_numbers):
                        print(f"\n  Expediente {i + 1}/{exp_count}: #{exp_number}")

                        # Collect stale blob URLs before clicking the tile
                        stale_blob_srcs: set = set()
                        try:
                            for loc in page.locator("iframe.PDFViewer__embedArea").all():
                                s = loc.get_attribute("src", timeout=300)
                                if s and s.startswith("blob:"):
                                    stale_blob_srcs.add(s)
                        except Exception:
                            pass

                        # Clear network-capture buffers for this expediente
                        captured_pdf_urls.clear()
                        captured_pdf_data.clear()

                        # Re-query tiles (DOM may refresh after go_back)
                        exp_tiles = page.locator(".caseEntryTile__simpleView")
                        if i >= exp_tiles.count():
                            print("    Tile no longer in DOM — skipping.")
                            continue

                        date_prefix     = f"{exp_dates[i]}_" if exp_dates[i] else ""
                        filename_prefix = f"{date_prefix}[{exp_number}]_{case_number}"

                        # Skip the tile click entirely if the Documento was
                        # already downloaded in a previous run.
                        if _already_downloaded(filename_prefix):
                            print(f"    Already downloaded — skipping.")
                            continue

                        try:
                            exp_tiles.nth(i).click()
                            try:
                                page.wait_for_selector(
                                    "button[title='Documento'],"
                                    " button[title='Notificación'],"
                                    " .caseEntryDocumentContainer",
                                    timeout=5000,
                                )
                            except Exception:
                                page.wait_for_timeout(1000)

                            _download_documento(
                                page, filename_prefix,
                                stale_blob_srcs,
                                captured_pdf_urls,
                                captured_pdf_data,
                            )

                            page.go_back()
                            page.wait_for_selector(
                                ".caseEntryTile__simpleView", timeout=5000
                            )
                        except Exception as e:
                            print(f"    Error: {e}")
                            try:
                                page.go_back()
                                page.wait_for_timeout(2000)
                            except Exception:
                                pass

                    print(f"\nCase {case_number} done.")
                    # Return to landing page for next case
                    page.goto(landing_url)
                    page.wait_for_timeout(3000)

                page.remove_listener("response", on_response)
                print("\nAll cases processed.")

        except Exception as e:
            print(f"\n[Error: {e.__class__.__name__}: {e}]")
        finally:
            _active_browser = None
            sys.stdout, sys.stderr = old_out, old_err

        self.after(0, self._on_done)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _on_done(self):
        self._log("\n[Done]\n")
        self._set_idle("Finished")

    def _set_idle(self, status: str):
        self._running = False
        self.start_btn.configure(
            text="▶   Start",
            fg_color=_CTK_BLUE,
            hover_color=_CTK_BLUE_HOVER,
            state="normal",
        )
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text=status, text_color="gray")

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
    app = SearchCasesApp()
    app.mainloop()
