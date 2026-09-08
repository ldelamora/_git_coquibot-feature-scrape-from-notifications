# sumac_login.py — Playwright automation for logging into SUMAC and scraping PDFs.
#
# ── Navigation hierarchy ──────────────────────────────────────────────────────
#   Level 1  →  Notifications landing page  (two panels of case tiles)
#   Level 2  →  Case detail view            (.caseEntryTile__simpleView expediente list)
#   Level 3  →  Expediente detail           (Documento / Notificación tabs + optional Anejo pills)
#
# Because SUMAC is a Single-Page App (SPA), navigating "back" between levels
# uses page.go_back() or a hard page.goto() — the DOM does not re-render
# predictably after a Playwright .click() alone.
#
# ── Expediente detail layout (Level 3) ───────────────────────────────────────
# When the expediente opens, SUMAC auto-selects the first Anejo pill and renders
# a two-column layout:
#
#   ┌─────────────────────────────┬──────────────────────────────┐
#   │  LEFT pillbox               │  RIGHT pillbox               │
#   │  .leftPillbox               │  .rightPillbox               │
#   │  Always shows the Documento │  Shows the currently-selected│
#   │  PDF (the main filing)      │  Anejo (attachment) PDF      │
#   │  [Download button]          │  [Download button]           │
#   └─────────────────────────────┴──────────────────────────────┘
#
# The two download buttons are visually identical (.caseEntryDocumentContainer__downloadButton)
# but scoped to their respective pillbox — so we always target the correct one
# by qualifying with `.leftPillbox` or `.rightPillbox`.
#
# ── PDF capture strategies ────────────────────────────────────────────────────
# Anejos:   (1) right-pillbox download button  (2) right-pillbox iframe src
# Documento:(0) left-pillbox download button while anejos are active
#           (1) dedicated .caseEntriesView__downloadButton after tab click
#           (2) generic anchor/download links
#           (3) URL captured passively from network responses (on_response)

import os
import sys
from pathlib import Path

# Point Playwright to the correct Chromium installation.
# When frozen: look next to the exe first (Dropbox-portable setup),
#              then fall back to standard AppData location.
# When running as a script: use standard AppData location.
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    if getattr(sys, 'frozen', False):
        _browsers = Path(sys.executable).parent / "ms-playwright"
        if not _browsers.exists():
            _browsers = Path.home() / "AppData" / "Local" / "ms-playwright"
    else:
        _browsers = Path.home() / "AppData" / "Local" / "ms-playwright"
    if _browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers)

from playwright.sync_api import sync_playwright
import re
import urllib.request
import time

# Entry point for the sign-in form.
SUMAC_URL = "https://tribunalelectronico.ramajudicial.pr/sumac2018/signIn.html"

# Plain-text credentials file (gitignored). Line 1 = username, line 2 = password.
CREDENTIALS_FILE = "sumac.txt"

# Processing order per expediente (see _process_expediente):
#   1. Documento   — left-pillbox button (no tab click; keeps anejo pillbox intact)
#   2. Anejos      — iterate pills while pillbox is still visible
#   3. Notificación — tab click (view changes, pillbox hidden; anejos already done)


def read_credentials():
    """Read username and password from the two-line credentials file."""
    with open(CREDENTIALS_FILE, "r") as f:
        lines = [line.strip() for line in f.readlines()]
    if len(lines) < 2:
        raise ValueError("sumac.txt must have username on line 1 and password on line 2")
    return lines[0], lines[1]


def _truncate_filename(fname, max_length=100):
    """
    Cap a filename at `max_length` characters, preserving its extension.
    If truncation is needed, "..." is inserted before the extension.
    """
    if len(fname) <= max_length:
        return fname
    stem, ext = os.path.splitext(fname)
    keep = max_length - len(ext) - 3  # room for "..." + extension
    return f"{stem[:keep]}...{ext}"


def _cookie_header(page):
    """
    Build a Cookie header string from the current browser session.
    Needed when downloading files via urllib (outside of Playwright) so that
    the server still recognises the authenticated session.
    """
    cookies = page.context.cookies()
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _save_pdf_from_url(page, url, save_path, pdf_data_cache=None, timeout=30):
    """
    Save a PDF to `save_path`.  Returns True on success.

    Checks `pdf_data_cache` first — bytes captured at response time require no
    second network request and work even for one-time-token URLs.  Falls back to
    a blob: in-browser read, then urllib for plain HTTP URLs.

    `timeout` (seconds) bounds only the last-resort urllib request.
    """
    import base64

    # Primary: bytes already captured when the network response first arrived.
    if pdf_data_cache and url in pdf_data_cache:
        data = pdf_data_cache[url]
        if data[:4] != b"%PDF":
            print(f"    cached response for {url} is not a PDF ({len(data)} bytes) — skipping")
            return False
        with open(save_path, "wb") as f:
            f.write(data)
        return True

    # Blob URLs live only in the browser — read them via page.evaluate().
    if url.startswith("blob:"):
        try:
            b64 = page.evaluate("""async (blobUrl) => {
                const resp = await fetch(blobUrl);
                const buf  = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.byteLength; i++)
                    binary += String.fromCharCode(bytes[i]);
                return btoa(binary);
            }""", url)
            with open(save_path, "wb") as f:
                f.write(base64.b64decode(b64))
            return True
        except Exception as e:
            print(f"    blob fetch failed for {url}: {e}")
        return False

    # Last resort: urllib re-download (slow; may fail for one-time-token URLs).
    try:
        req = urllib.request.Request(url, headers={"Cookie": _cookie_header(page)})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                with open(save_path, "wb") as f:
                    f.write(resp.read())
                return True
    except Exception as e:
        print(f"    urllib download failed for {url}: {e}")
    return False


def _remaining_ms(deadline, default_ms):
    """Milliseconds left before `deadline` (clamped to a 300 ms floor), or
    `default_ms` if there is no deadline (Notificación/Anejo — unaffected)."""
    if deadline is None:
        return default_ms
    return max(300, int((deadline - time.time()) * 1000))


def _remaining_s(deadline, default_s):
    """Seconds left before `deadline` (clamped to a 1 s floor), or `default_s`
    if there is no deadline (Notificación/Anejo — unaffected)."""
    if deadline is None:
        return default_s
    return max(1, deadline - time.time())


def _already_downloaded(prefix):
    """Return True if sumac_documents already contains a file whose name starts with
    prefix, comparing only the first 80 characters."""
    dest = Path("sumac_documents")
    if not dest.exists():
        return False
    prefix = prefix[:80]
    return any(f.name[:80].startswith(prefix) for f in dest.iterdir() if f.is_file())


def _cleanup_old_pdfs(months=6):
    """Delete PDFs in sumac_documents whose file save time is older than `months`
    months ago (average 30.44-day month), keeping the folder from growing unbounded."""
    dest = Path("sumac_documents")
    if not dest.exists():
        return
    cutoff = time.time() - months * 30.44 * 24 * 60 * 60
    deleted = 0
    for f in dest.iterdir():
        if f.is_file() and f.suffix.lower() == ".pdf":
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except Exception as e:
                print(f"    [cleanup] Failed to delete {f.name}: {e}")
    print(f"[cleanup] Deleted {deleted} PDF(s) saved more than {months} months ago.")



MESES = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
    # 3-letter abbreviations used in the Level 2 date block
    "ene": "01", "feb": "02", "mar": "03", "abr": "04",
    "may": "05", "jun": "06", "jul": "07", "ago": "08",
    "sep": "09", "oct": "10", "nov": "11", "dic": "12",
}


def _wait_for_all_tiles(page, timeout=5000):
    """Wait for both the top (home__topLeftPanel) and bottom (home__bottomLeftPanel) tiles."""
    try:
        page.wait_for_selector(".home__topLeftPanel .courtNotificationsBox__tile", timeout=timeout)
    except Exception:
        pass
    try:
        page.wait_for_selector(".home__bottomLeftPanel .partiesNotificationsBox__tile", timeout=timeout)
    except Exception:
        pass  # Bottom panel may be empty — not an error



def _download_documento_pillbox(page, filename_prefix, captured_pdf_data, stale_blob_srcs=None, deadline=None):
    """
    Download a PDF via the .caseEntryDocumentContainer left-pillbox download
    button/iframe — the layout used both by a regular expediente's Documento
    tab (when anejos are active) and by a Tribunal Apelativo (TA) recourse
    case's docket-entry detail view, which uses the identical container.

    Returns:
      True  — PDF saved.
      False — the pillbox is present but has no downloadable PDF (a text-only
              ORDEN/ENTERADO). Callers must NOT fall back to other strategies
              here — doing so risks grabbing an unrelated document's bytes.
      None  — the pillbox is absent entirely. Callers should try their own
              fallback strategies (e.g. a tab click).
    """
    left_dl_btn = page.locator(
        ".caseEntryDocumentContainer__leftPillbox"
        " .caseEntryDocumentContainer__downloadButton"
    )
    if left_dl_btn.count() == 0:
        return None

    left_title = ""
    try:
        h1 = page.locator(
            ".caseEntryDocumentContainer__leftPillbox"
            " .caseEntryDocumentContainer__documentHeader h1"
        ).first
        left_title = (h1.get_attribute("title", timeout=_remaining_ms(deadline, 1000)) or h1.inner_text(timeout=_remaining_ms(deadline, 1000)) or "").strip()
        left_title = re.sub(r'[\\/:*?"<>|.]+', '', left_title).strip()
        left_title = re.sub(r'\s+', ' ', left_title)[:50]
    except Exception:
        left_title = ""
    title_part = f" - {left_title}" if left_title else ""
    try:
        with page.expect_download(timeout=_remaining_ms(deadline, 5000)) as dl_info:
            left_dl_btn.click(timeout=_remaining_ms(deadline, 30000))
        dl = dl_info.value
        fname = f"{filename_prefix}{title_part}.pdf"
        save_path = os.path.join("sumac_documents", _truncate_filename(fname))
        dl.save_as(save_path)
        print(f"    [Documento] Saved from left pillbox: {save_path}")
        return True
    except Exception as e:
        print(f"    [Documento] Left pillbox button failed: {e}")

    # Strategy 0b: the button sometimes opens the PDF inline rather than
    # triggering a browser download.  Read the blob URL directly from the
    # left-pillbox iframe — same technique used for anejos' right pillbox.
    # Guard: only use the blob URL if the iframe is VISIBLE (not hidden) and
    # not in stale_blob_srcs (blobs from previous expedientes persist in
    # browser memory and may still appear in hidden DOM nodes).
    try:
        left_iframe_loc = page.locator(
            ".caseEntryDocumentContainer__leftPillbox iframe.PDFViewer__embedArea"
        )
        left_url = None
        if left_iframe_loc.count() > 0 and left_iframe_loc.first.is_visible():
            left_url = left_iframe_loc.first.get_attribute("src", timeout=_remaining_ms(deadline, 2000))
        if left_url and (stale_blob_srcs is None or left_url not in stale_blob_srcs):
            fname = f"{filename_prefix}{title_part}.pdf"
            save_path = os.path.join("sumac_documents", _truncate_filename(fname))
            if _save_pdf_from_url(page, left_url, save_path, captured_pdf_data, timeout=_remaining_s(deadline, 30)):
                print(f"    [Documento] Saved from left pillbox iframe: {save_path}")
                return True
    except Exception:
        pass

    # Both 0a (button) and 0b (iframe src) failed while the left pillbox
    # is present.  The entry has no downloadable PDF (e.g. a text-only
    # ORDEN/ENTERADO).
    print(f"    [Documento] Left pillbox present but no PDF found — skipping.")
    return False


def _download_from_tab(page, tab_name, filename_prefix, captured_pdf_urls, captured_pdf_data,
                       stale_blob_srcs=None):
    """
    Download the PDF for a given tab (Notificación or Documento) inside an
    expediente detail view.  Returns True as soon as one PDF is saved, False
    if nothing was found.

    For Documento, four strategies are tried in order:
      0. Left-pillbox download button — only present when anejos are active;
         unambiguously downloads the Documento without clicking the tab at all.
      1. Dedicated download button (.caseEntriesView__downloadButton) after
         clicking the tab.
      2. Any generic anchor / download link on the page.
      3. PDF URL intercepted passively from network traffic (on_response in
         scrape_all_pdfs) — fallback for PDFs rendered inline via PDF.js.

    For Notificación, only strategies 1–3 apply (no left-pillbox button).

    stale_blob_srcs — set of blob: URLs that were already in the browser's DOM
                      before the current expediente was opened.  Any blob URL
                      found in this set is from a previous expediente's render
                      and must be skipped to prevent cross-expediente contamination.
    """
    # Locate the tab button — SUMAC sometimes uses a title attribute, sometimes
    # just inner text, so we fall back to text-based filtering.
    tab = page.locator(f"button[title='{tab_name}']")
    if tab.count() == 0:
        tab = page.locator("button").filter(has_text=tab_name)
    if tab.count() == 0:
        print(f"    Tab '{tab_name}' not found.")
        return False

    # Omit the tab name from Documento filenames — the doc title already identifies it.
    tab_label = "" if tab_name == "Documento" else tab_name

    # Documento gets a hard 5 s wall-clock budget covering every strategy below
    # (including the left-pillbox path and any urllib fallback download): once it
    # expires we give up and move on, no matter which strategy is mid-flight.
    # None for Notificación, so every budget check further down is a no-op there.
    documento_deadline = (time.time() + 5.0) if not tab_label else None

    # Skip if already saved in a prior run.
    # Notificación filenames start with "{prefix}_Notificación", so a simple
    # prefix check works.  Documento filenames have no extra segment after the
    # prefix — they look like "{prefix} - Title.pdf" — so we check for a space
    # or dot immediately after the prefix to avoid false matches with anejos
    # (which would also start with the same prefix).
    if tab_label:
        already_dl = _already_downloaded(f"{filename_prefix}_{tab_label}")
    else:
        _dest_dl = Path("sumac_documents")
        already_dl = _dest_dl.exists() and any(
            f.name.startswith(filename_prefix)
            and len(f.name) > len(filename_prefix)
            and f.name[len(filename_prefix)] in ('.', ' ')
            for f in _dest_dl.iterdir() if f.is_file()
        )
    if already_dl:
        print(f"    [{tab_name}] Already downloaded, skipping.")
        return True

    # ── Strategy 0 (Documento only): left-pillbox download button ───────────────
    # While anejos are active, the page has a two-column layout: Documento on the
    # left, the selected Anejo on the right.  The left-side download button is
    # unambiguously tied to the Documento PDF — no need to click the tab or guess
    # which captured URL belongs to which document. Extracted into
    # _download_documento_pillbox since Tribunal Apelativo docket entries use
    # the identical container.
    if not tab_label:
        result = _download_documento_pillbox(page, filename_prefix, captured_pdf_data,
                                              stale_blob_srcs, documento_deadline)
        if result is not None:
            # True (saved) or False (pillbox present, no PDF) — either way, do
            # NOT fall through to the tab-click fallback: falling through when
            # the pillbox is present but empty would fire Strategy 3's
            # stale-URL fallback and save the wrong bytes.
            return result
        # result is None — pillbox absent entirely — fall through below.

    # ── Tab-click fallback (Strategy 0 was absent or unavailable) ─────────────
    # Snapshot captured URLs before clicking so we can identify what fires new.
    # We do NOT clear the list: if SUMAC serves the Documento from cache on
    # re-click (no new network request), the pre-click URL is the only handle we
    # have for Strategy 3.
    urls_before_click = list(captured_pdf_urls)
    if documento_deadline:
        # Documento: cap the click's own actionability wait to the remaining
        # budget — Playwright's default 30 s wait here would otherwise bypass
        # every deadline check below if the tab isn't immediately clickable.
        try:
            tab.first.click(timeout=_remaining_ms(documento_deadline, 30000))
        except Exception as e:
            print(f"    [Documento] Tab click failed/timed out: {e}")
            return False
    else:
        tab.first.click()

    # Two-phase poll after the tab click:
    #   Phase 1 (0–2 s): watch for a new PDF URL to appear in captured_pdf_urls.
    #     If nothing arrives the PDF was probably served from cache — stop early
    #     and fall through to Strategy 3 with the pre-existing URL.
    #   Phase 2 (2–15 s): URL appeared but response.body() hasn't been cached yet
    #     (it runs in a background thread).  Keep waiting so Strategy 3 can use
    #     the in-memory bytes instead of a urllib re-download, which often fails
    #     for one-time-token URLs.
    # Each iteration also checks for id="Nohaynotificaciones" so that an empty
    # notification tab is detected as soon as SUMAC renders it — regardless of
    # how long the SPA takes — without burning through all 15 seconds.
    for i in range(75):  # 75 × 200 ms = 15 s ceiling
        if documento_deadline and time.time() > documento_deadline:
            print(f"    [Documento] 5 s time budget exceeded — moving on.")
            return False
        page.wait_for_timeout(200)
        if tab_label:
            try:
                if (page.locator("#Nohaynotificaciones").is_visible()
                        or page.locator("p.emptyContainerMessage").is_visible()):
                    print(f"    [{tab_name}] No hay notificaciones — skipping.")
                    return False
            except Exception:
                pass
        new = [u for u in captured_pdf_urls if u not in urls_before_click]
        if new and any(u in captured_pdf_data for u in new):
            break  # URL + bytes cached — ready to save
        if not new and i >= 9:
            break  # no new URL after 2 s — fall through to Strategy 1/2/3
        if new and i >= 60:
            break  # URL found but body never cached after 12 s — try anyway

    # Post-loop guard: the polling loop only runs for 2 s before breaking on
    # "no new URL".  If SUMAC rendered the empty state slowly, or if is_visible()
    # returned False due to CSS (e.g. opacity/height tricks), we may have missed it.
    # Use count() > 0 here — DOM presence alone is sufficient because SUMAC only
    # inserts #Nohaynotificaciones when there genuinely are no notifications.
    if tab_label:
        try:
            if (page.locator("#Nohaynotificaciones").count() > 0
                    or page.locator("p.emptyContainerMessage").count() > 0):
                print(f"    [{tab_name}] No hay notificaciones — skipping.")
                return False
        except Exception:
            pass

    # Read the document title shown in the tab header.  Use a tab-specific
    # selector so a hidden Documento header never bleeds into Notificación.
    doc_title = ""
    try:
        if tab_label:
            # Notificación: title is in the notification PDF viewer heading.
            h1 = page.locator(
                ".caseEntryNotificationsContainer__mainPillbox h1"
            ).first
        else:
            h1 = page.locator(".caseEntryDocumentContainer__documentHeader h1").first
        doc_title = (h1.get_attribute("title", timeout=_remaining_ms(documento_deadline, 30000)) or h1.inner_text(timeout=_remaining_ms(documento_deadline, 1000)) or "").strip()
        # Sanitize: remove characters illegal in filenames, collapse whitespace.
        doc_title = re.sub(r'[\\/:*?"<>|.]+', '', doc_title).strip()
        doc_title = re.sub(r'\s+', ' ', doc_title)[:50]
     
    except Exception:
        doc_title = ""

    title_part = f" - {doc_title}" if doc_title else ""

    # If the network listener already captured a new URL during the poll above,
    # jump straight to Strategy 3 — no point trying download buttons that would
    # each burn a 1 s timeout on a PDF that already arrived via the network.
    new_urls_after_wait = [u for u in captured_pdf_urls if u not in urls_before_click]
    if documento_deadline and time.time() > documento_deadline:
        print(f"    [Documento] 5 s time budget exceeded — moving on.")
        return False
    if not new_urls_after_wait:
        # ── Strategy 0c: read PDF viewer iframe src directly ──────────────────
        # When SUMAC serves the PDF from browser cache (no new network request
        # fires), the iframe may already be showing the correct PDF.
        # Two safety guards prevent reading stale content from previous expedientes:
        #   1. Container-specific selector — Notificación iframes live in
        #      caseEntryNotificationsContainer__mainPillbox; using that selector
        #      avoids accidentally reading a hidden left-pillbox iframe that
        #      SUMAC's SPA left in the DOM from an earlier expediente.
        #   2. is_visible() — hidden iframes (display:none) are from prior views.
        #   3. stale_blob_srcs check — blob: URLs that existed before this
        #      expediente was opened belong to previous expediente renders.
        try:
            if tab_label:
                # Notificación: scope to the notification-specific container.
                iframe_sel = (
                    ".caseEntryNotificationsContainer__mainPillbox"
                    " iframe.PDFViewer__embedArea"
                )
            else:
                # Documento (no-anejos path): use any visible iframe.
                iframe_sel = "iframe.PDFViewer__embedArea"
            tab_iframe_loc = page.locator(iframe_sel).first
            tab_iframe_url = None
            if tab_iframe_loc.count() > 0 and tab_iframe_loc.is_visible():
                tab_iframe_url = tab_iframe_loc.get_attribute("src", timeout=_remaining_ms(documento_deadline, 2000))
            if tab_iframe_url and (stale_blob_srcs is None or tab_iframe_url not in stale_blob_srcs):
                fname = f"{filename_prefix}_{tab_label}{title_part}.pdf" if tab_label else f"{filename_prefix}{title_part}.pdf"
                save_path = os.path.join("sumac_documents", _truncate_filename(fname))
                if _save_pdf_from_url(page, tab_iframe_url, save_path, captured_pdf_data, timeout=_remaining_s(documento_deadline, 30)):
                    print(f"    [{tab_name}] Saved from iframe src: {save_path}")
                    return True
        except Exception:
            pass

        # ── Strategy 1: dedicated download button ─────────────────────────────
        # Use a tab-specific selector so we never accidentally click a hidden
        # Documento button while downloading Notificación (SUMAC's SPA hides
        # elements with CSS but keeps them in the DOM).
        if tab_label:
            dl_btn = page.locator(".caseEntryReceiptContainer__downloadButton")
        else:
            dl_btn = page.locator(".caseEntriesView__downloadButton")
        if dl_btn.count() > 0:
            for j in range(dl_btn.count()):
                if documento_deadline and time.time() > documento_deadline:
                    print(f"    [Documento] 5 s time budget exceeded — moving on.")
                    return False
                try:
                    print(f"    [{tab_name}] Clicking download button {j+1}...")
                    with page.expect_download(timeout=1000) as dl_info:
                        dl_btn.nth(j).click(timeout=_remaining_ms(documento_deadline, 30000))
                    dl = dl_info.value
                    fname = f"{filename_prefix}_{tab_label}{title_part}.pdf" if tab_label else f"{filename_prefix}{title_part}.pdf"
                    save_path = os.path.join("sumac_documents", _truncate_filename(fname))
                    dl.save_as(save_path)
                    print(f"    Saved: {save_path}")
                    return True
                except Exception as e:
                    print(f"    Download button failed: {e}")

        # ── Strategy 2: generic download anchors ──────────────────────────────
        # is_visible() filters out anchors from hidden tab views (SUMAC's SPA
        # hides rather than removes the inactive tab's DOM).
        for selector in ["a[download]", "a[href*='.pdf']"]:
            elems = page.locator(selector)
            if elems.count() > 0:
                for j in range(elems.count()):
                    if documento_deadline and time.time() > documento_deadline:
                        print(f"    [Documento] 5 s time budget exceeded — moving on.")
                        return False
                    try:
                        if not elems.nth(j).is_visible():
                            continue
                        with page.expect_download(timeout=1000) as dl_info:
                            elems.nth(j).click(timeout=_remaining_ms(documento_deadline, 30000))
                        dl = dl_info.value
                        fname = f"{filename_prefix}_{tab_label}{title_part}.pdf" if tab_label else f"{filename_prefix}{title_part}.pdf"
                        save_path = os.path.join("sumac_documents", _truncate_filename(fname))
                        dl.save_as(save_path)
                        print(f"    Saved: {save_path}")
                        return True
                    except Exception:
                        pass

    # ── Strategy 3: intercepted network URL ──────────────────────────────────
    # Prefer URLs that arrived after the tab click (fresh request).
    #
    # Documento: fall back to pre-click captured URLs when SUMAC serves the PDF
    # from its own SPA cache (no new network request fires after the tab click).
    # The fallback list is reversed so the most-recent URL is tried first.
    #
    # Notificación: NEVER fall back to pre-click captured URLs.  The buffer is
    # cleared in _process_expediente immediately before this call, so any URL
    # already in captured_pdf_urls when we reach this point is a delayed HTTP
    # response from an anejo or Documento download that arrived after the clear —
    # using it would save the wrong PDF under the Notificación filename.
    #
    # Consume the URL afterwards so it cannot bleed into the next tab's fallback.
    new_urls = new_urls_after_wait or [u for u in captured_pdf_urls if u not in urls_before_click]
    if tab_label:
        # Notificación: only use URLs that arrived after the tab click.
        urls_to_try = new_urls
    else:
        # Documento: fall back to pre-tab-click URLs when SUMAC serves from its
        # own SPA cache (no new network request fires).  Use urls_before_click
        # (not all of captured_pdf_urls) so any speculative post-click responses
        # — such as a Notificación PDF prefetched by SUMAC — are excluded.
        urls_to_try = new_urls if new_urls else list(reversed(urls_before_click))
    for j, url in enumerate(urls_to_try):
        if documento_deadline and time.time() > documento_deadline:
            print(f"    [Documento] 5 s time budget exceeded — moving on.")
            return False
        fname = f"{filename_prefix}_{tab_label}{title_part}.pdf" if tab_label else f"{filename_prefix}{title_part}.pdf"
        save_path = os.path.join("sumac_documents", _truncate_filename(fname))
        print(f"    [{tab_name}] Saving intercepted PDF: {url}")
        if _save_pdf_from_url(page, url, save_path, captured_pdf_data, timeout=_remaining_s(documento_deadline, 30)):
            captured_pdf_urls[:] = [u for u in captured_pdf_urls if u != url]
            captured_pdf_data.pop(url, None)
            print(f"    Saved: {save_path}")
            return True

    print(f"    [{tab_name}] No PDF found.")
    return False


def _download_anejo_attachments(page, filename_prefix, captured_pdf_data, session_blob_srcs=None):
    """
    Download all Anejo (attachment) PDFs inside an expediente detail view.

    SUMAC layout when an Anejo pill is selected:
      - A horizontal scrollable pill bar at the top lists all attachments.
      - The main area splits into two columns:
          LEFT  (.leftPillbox)  — always shows the Documento PDF
          RIGHT (.rightPillbox) — shows the currently-selected Anejo PDF
      - Each column has its own download button.

    Approach:
      1. Iterate pills first-to-last.  SUMAC auto-selects the first pill on
         load, so j=0 is already showing in the right pillbox — we skip the
         click to avoid accidentally deselecting it (clicking a selected pill
         in SUMAC toggles it off and reloads the Documento instead).
      2. For each pill (after clicking it when needed), try the right-pillbox
         download button (Strategy A).  Fall back to reading the right-pillbox
         iframe src directly (Strategy B).

    Filenames follow the pattern: <prefix>_anejo_<n> - <label>_<original>.pdf
    """
    # BEM class confirmed from DOM inspection.
    container = page.locator("div.caseEntryDocumentContainer__attachmentsPillbox")
    if container.count() == 0:
        # No Anejo section present on this expediente — nothing to do.
        return

    # Scroll the inner scrollable area fully right then back to the start
    # so any lazy-rendered pills outside the viewport are forced into the DOM.
    # The pills sit in attachmentsPillboxScrollArea (horizontal scroll).
    scroll_area = container.locator(".caseEntryDocumentContainer__attachmentsPillboxScrollArea")
    if scroll_area.count() > 0:
        # Only attempt this when the scroll area actually exists — otherwise
        # .evaluate() auto-waits up to Playwright's 30 s default for it to
        # appear before giving up, stalling every no-attachments expediente.
        try:
            scroll_area.first.evaluate("el => { el.scrollLeft = el.scrollWidth; }", timeout=1000)
            page.wait_for_timeout(300)
            scroll_area.first.evaluate("el => { el.scrollLeft = 0; }", timeout=1000)
            page.wait_for_timeout(200)
        except Exception:
            pass

    # Each attachment is rendered as a .caseEntryDocumentContainer__attachmentTile.
    pills = container.locator(".caseEntryDocumentContainer__attachmentTile")
    pill_count = pills.count()
    if pill_count == 0:
        print(f"    [Anejo] Container found but no attachment tiles inside.")
        return

    print(f"    [Anejo] Found {pill_count} attachment(s).")

    # Iterate first-to-last: clicking pill 0 first naturally de-selects any
    # pre-selected last pill (SUMAC auto-selects the last anejo on load).
    # By the time we reach the last pill it is no longer selected, so clicking
    # it loads its own PDF instead of re-firing the Documento blob.
    for j in range(pill_count):
        # Skip if this attachment index was already saved in a prior run.
        # Use a boundary check: the character after the number must be non-digit
        # so that e.g. anejo_1 does not falsely match anejo_10, anejo_11, etc.
        _anejo_prefix = f"{filename_prefix}_anejo_{j + 1}"
        _dest = Path("sumac_documents")
        if _dest.exists() and any(
            f.name.startswith(_anejo_prefix)
            and not f.name[len(_anejo_prefix):len(_anejo_prefix) + 1].isdigit()
            for f in _dest.iterdir() if f.is_file()
        ):
            print(f"    [Anejo] Attachment {j + 1} already downloaded, skipping.")
            continue

        # Read the pill label BEFORE clicking — it may change or disappear after.
        try:
            raw_label = pills.nth(j).inner_text(timeout=1000).strip()
        except Exception:
            raw_label = ""
        # Sanitize for use in a filename: collapse whitespace, remove illegal chars, truncate.
        pill_label = re.sub(r'[\\/:*?"<>|]+', '', raw_label).strip()
        pill_label = re.sub(r'\s+', ' ', pill_label)[:50]
        label_part = f" - {pill_label}" if pill_label else ""

        try:
            is_selected = pills.nth(j).evaluate(
                "el => el.closest('.selectableTile__view')"
                ".classList.contains('selectableTile__view-selected')"
            )
        except Exception:
            is_selected = False

        if not is_selected:
            print(f"    [Anejo] Clicking attachment {j + 1}/{pill_count}: '{pill_label}'...")
            pills.nth(j).evaluate("el => el.closest('.selectableTile__view').click()")
            # Wait for the right pillbox to update with this anejo's PDF.
            page.wait_for_timeout(5000)
        else:
            print(f"    [Anejo] Attachment {j + 1}/{pill_count}: '{pill_label}' (pre-selected)")

        # ── Strategy A: right-pillbox download button ─────────────────────────
        right_dl_btn = page.locator(
            ".caseEntryDocumentContainer__rightPillbox .caseEntryDocumentContainer__downloadButton"
        )
        if right_dl_btn.count() > 0:
            try:
                with page.expect_download(timeout=5000) as dl_info:
                    right_dl_btn.click()
                dl = dl_info.value
                # Strip whatever extension the server provides and force .pdf —
                # SUMAC sometimes omits the extension or sends a non-pdf suffix.
                base = Path(dl.suggested_filename).stem if dl.suggested_filename else "attachment"
                fname = f"{filename_prefix}_anejo_{j + 1}{label_part}_{base}.pdf"
                save_path = os.path.join("sumac_documents", _truncate_filename(fname))
                dl.save_as(save_path)
                print(f"    [Anejo] Saved: {save_path}")
                page.wait_for_timeout(2000)
                continue
            except Exception as e:
                print(f"    [Anejo] Right download button failed: {e}")

        # ── Strategy B: right-pillbox iframe src (blob URL) ──────────────────
        # Guards mirror Strategy 0b/0c in _download_from_tab:
        #   1. is_visible() — the right pillbox may still hold the previous
        #      anejo's iframe in the DOM after a click; a hidden iframe means
        #      the render has not updated yet and the src is stale.
        #   2. session_blob_srcs — rejects blob URLs from previous expedientes
        #      that the SPA keeps alive in browser memory.
        #   3. Pop from captured_pdf_data on success — prevents the byte cache
        #      entry from bleeding into later Strategy 3 fallbacks.
        try:
            right_iframe_loc = page.locator(
                ".caseEntryDocumentContainer__rightPillbox iframe.PDFViewer__embedArea"
            )
            anejo_url = None
            if right_iframe_loc.count() > 0 and right_iframe_loc.first.is_visible():
                anejo_url = right_iframe_loc.first.get_attribute("src", timeout=5000)
        except Exception:
            anejo_url = None

        if anejo_url and (session_blob_srcs is None or anejo_url not in session_blob_srcs):
            fname = f"{filename_prefix}_anejo_{j + 1}{label_part}.pdf"
            save_path = os.path.join("sumac_documents", _truncate_filename(fname))
            print(f"    [Anejo] Saving from right pillbox iframe: {anejo_url}")
            if _save_pdf_from_url(page, anejo_url, save_path, captured_pdf_data):
                captured_pdf_data.pop(anejo_url, None)
                print(f"    [Anejo] Saved: {save_path}")
                page.wait_for_timeout(2000)
                continue

        print(f"    [Anejo] Could not save attachment {j + 1}.")


def _process_expediente(page, exp_idx, case_number, exp_number, exp_date, captured_pdf_urls, captured_pdf_data,
                        session_blob_srcs=None):
    """
    Level 3: download all PDFs from one expediente, then return to Level 2.

    Order matters:
      1. Anejos first — the anejo pillbox is only visible in the default
         expediente view.  Clicking any tab collapses it.
      2. Tabs (Notificación, Documento) after anejos.  Documento is downloaded
         via the left-pillbox button while the anejo view is still active;
         it falls back to a tab click only for expedientes without anejos.

    exp_idx   — zero-based tile index in the current DOM (re-queried here
                because prior navigation may have refreshed the list).
    exp_number — human-readable expediente number embedded in saved filenames.
    captured_pdf_urls / captured_pdf_data — shared network-capture buffers
                passed to _download_from_tab for Strategy 3 fallback.
    session_blob_srcs — set that accumulates every blob: URL seen across the
                entire session (all cases, all expedientes).  Any blob that
                existed before the current expediente tile click is stale and
                must not be reused — this prevents cross-case contamination
                because the SPA keeps old blob objects alive in browser memory
                even after navigating to a different case.
    """
    # Re-query tiles here because navigating back from a previous expediente
    # can trigger a DOM refresh, potentially invalidating stale locators.
    tiles = page.locator(".caseEntryTile__simpleView")
    if exp_idx >= tiles.count():
        print(f"  Expediente tile {exp_idx} no longer in DOM, skipping.")
        return

    t_exp_start = time.time()
    print(f"  Expediente {exp_idx + 1}: #{exp_number}  @ {time.strftime('%H:%M:%S')}")
    # Accumulate every blob: URL currently in the DOM into the session-wide
    # stale set before navigating into this expediente.  SUMAC's SPA keeps old
    # blob objects alive in browser memory across SPA navigation — even across
    # different cases — so any blob that exists NOW (before the tile click)
    # belongs to a previous render and must not be reused for this expediente.
    if session_blob_srcs is None:
        session_blob_srcs = set()
    try:
        for loc in page.locator("iframe.PDFViewer__embedArea").all():
            s = loc.get_attribute("src", timeout=300)
            if s and s.startswith("blob:"):
                session_blob_srcs.add(s)
    except Exception:
        pass
    # Clear network-capture buffers from previous expedientes so that Strategy 3's
    # fallback only ever sees URLs from the current expediente.
    captured_pdf_urls.clear()
    captured_pdf_data.clear()
    tiles.nth(exp_idx).click()
    # Wait until the expediente detail renders (tab buttons or document container appear).
    # Falls back to a short fixed wait if those selectors never show up.
    try:
        page.wait_for_selector(
            "button[title='Documento'], button[title='Notificación'], .caseEntryDocumentContainer",
            timeout=5000,
        )
    except Exception:
        page.wait_for_timeout(1000)
    print(f"    [timing] expediente rendered at +{time.time() - t_exp_start:.1f}s")

    # Confirm the case number from the page heading before building the prefix.
    # The same .caseViewHeading__mainHeading heading shown on the case list is
    # also present here; its title attribute is "{case_number} | {parties}".
    # Reading it here ensures the filename uses what SUMAC is actually showing,
    # not whatever was passed in from the notification tile.
    try:
        heading_title = page.locator(".caseViewHeading__mainHeading").first.get_attribute("title", timeout=3000) or ""
        page_case_number = heading_title.split(" | ")[0].strip()
        if page_case_number and page_case_number != case_number:
            print(f"    Page shows case {page_case_number} (expected {case_number}) — using page value.")
            case_number = page_case_number
    except Exception:
        pass

    # Build the filename prefix: date first so files sort chronologically.
    date_prefix = f"{exp_date}_" if exp_date else ""
    filename_prefix = f"{date_prefix}[{exp_number}]_{case_number}"

    # 1. Documento first — uses the left-pillbox download button, which is
    #    present as soon as the expediente opens (SUMAC auto-selects the first
    #    anejo, showing the two-column layout).  No tab click is needed, so
    #    the anejo pillbox stays intact for step 2.  For expedientes without
    #    anejos the left pillbox is absent and the function falls back to a
    #    tab click automatically.
    _download_from_tab(page, "Documento", filename_prefix, captured_pdf_urls, captured_pdf_data,
                       session_blob_srcs)
    print(f"    [timing] Documento done at +{time.time() - t_exp_start:.1f}s")

    # 2. Anejos — the pillbox is still visible because no tab was clicked above.
    _download_anejo_attachments(page, filename_prefix, captured_pdf_data, session_blob_srcs)
    print(f"    [timing] Anejo done at +{time.time() - t_exp_start:.1f}s")

    # 3. Notificación last — clicking its tab changes the view (pillbox gone),
    #    but anejos are already finished by this point.
    #    Add any new blob URLs that loaded during Documento/anejos into the
    #    session stale set so they cannot bleed into Notificación.
    try:
        for loc in page.locator("iframe.PDFViewer__embedArea").all():
            s = loc.get_attribute("src", timeout=300)
            if s and s.startswith("blob:"):
                session_blob_srcs.add(s)
    except Exception:
        pass
    # Clear network-capture buffers, then pause briefly so any HTTP responses
    # still in-flight from Documento/anejo downloads can arrive and be discarded.
    # Without the wait, a delayed anejo response could arrive between the clear
    # and the Notificación tab click, sneak into captured_pdf_urls, and be tried
    # as the notification URL.  The second clear removes anything that landed
    # during the drain wait.
    captured_pdf_urls.clear()
    captured_pdf_data.clear()
    page.wait_for_timeout(400)
    captured_pdf_urls.clear()
    captured_pdf_data.clear()
    _download_from_tab(page, "Notificación", filename_prefix, captured_pdf_urls, captured_pdf_data,
                       session_blob_srcs)
    print(f"    [timing] Notificación done at +{time.time() - t_exp_start:.1f}s")

    # Return to case detail (Level 2) using browser history.
    # wait_for_selector ensures the expediente list is ready before the caller
    # tries to access the next tile index.
    page.go_back()
    # state="visible" — hidden residual tiles from the expediente view must not
    # satisfy this check; we need the case-detail tile list to be genuinely visible.
    page.wait_for_selector(".caseEntryTile__simpleView", state="visible", timeout=5000)
    print(f"    [timing] expediente {exp_idx + 1} total: {time.time() - t_exp_start:.1f}s")


def _process_recourse_docket_entry(page, docket_idx, case_number, docket_number, docket_date,
                                   captured_pdf_data, session_blob_srcs=None):
    """
    Tribunal Apelativo (TA / recourse) case: download the PDF for one row of
    the docket list (.recourseDocketEntryTile__view), then return to the list.

    Clicking a docket entry opens a detail view using the identical
    .caseEntryDocumentContainer layout as a regular expediente's Documento
    tab, so _download_documento_pillbox is reused as-is.

    docket_idx    — zero-based tile index in the current DOM (re-queried here
                    because prior navigation may have refreshed the list).
    docket_number — the docket entry's sequence number, embedded in saved filenames.
    session_blob_srcs — same cross-entry stale-blob guard used by _process_expediente.
    """
    tiles = page.locator(".recourseDocketEntryTile__view")
    if docket_idx >= tiles.count():
        print(f"  Docket entry tile {docket_idx} no longer in DOM, skipping.")
        return

    t_entry_start = time.time()
    print(f"  Docket entry {docket_idx + 1}: #{docket_number}  @ {time.strftime('%H:%M:%S')}")

    if session_blob_srcs is None:
        session_blob_srcs = set()
    try:
        for loc in page.locator("iframe.PDFViewer__embedArea").all():
            s = loc.get_attribute("src", timeout=300)
            if s and s.startswith("blob:"):
                session_blob_srcs.add(s)
    except Exception:
        pass

    tiles.nth(docket_idx).click()
    try:
        page.wait_for_selector(".caseEntryDocumentContainer", state="visible", timeout=5000)
    except Exception:
        page.wait_for_timeout(1000)
    print(f"    [timing] docket entry rendered at +{time.time() - t_entry_start:.1f}s")

    date_prefix = f"{docket_date}_" if docket_date else ""
    filename_prefix = f"{date_prefix}[{docket_number}]_{case_number}"

    # Skip if already saved in a prior run — same convention as Documento
    # filenames (no extra tab-name segment after the prefix).
    dest_dl = Path("sumac_documents")
    already_dl = dest_dl.exists() and any(
        f.name.startswith(filename_prefix)
        and len(f.name) > len(filename_prefix)
        and f.name[len(filename_prefix)] in ('.', ' ')
        for f in dest_dl.iterdir() if f.is_file()
    )
    if already_dl:
        print(f"    Already downloaded, skipping.")
    else:
        # Same 5 s budget as a regular Documento download.
        result = _download_documento_pillbox(page, filename_prefix, captured_pdf_data,
                                             session_blob_srcs, deadline=time.time() + 5.0)
        if result is None:
            print(f"    No document container found for this docket entry.")
        elif result is False:
            print(f"    No PDF found for this docket entry.")
    print(f"    [timing] docket entry done at +{time.time() - t_entry_start:.1f}s")

    page.go_back()
    page.wait_for_selector(".recourseDocketEntryTile__view", state="visible", timeout=5000)
    print(f"    [timing] docket entry {docket_idx + 1} total: {time.time() - t_entry_start:.1f}s")


def _process_recourse_case(page, case_number, captured_pdf_data, session_blob_srcs=None):
    """
    Level 2 (Tribunal Apelativo variant): snapshot every row of a recourse
    case's docket list (.recourseDocketEntryTile__view), then download each
    one's PDF via _process_recourse_docket_entry.

    Called in place of the regular expediente loop when a case's detail view
    uses the recourse docket layout instead of .caseEntryTile__simpleView.
    """
    # Read the authoritative case number from the recourse-specific heading.
    # Format: "{case_number} | {parties}", same convention as the regular
    # .caseViewHeading__mainHeading but under a different class name.
    try:
        heading_title = page.locator(".recourseHeader__mainHeading").first.get_attribute("title", timeout=3000) or ""
        page_case_number = heading_title.split(" | ")[0].strip()
        if page_case_number and page_case_number != case_number:
            print(f"    Page shows case {page_case_number} (expected {case_number}) — using page value.")
            case_number = page_case_number
    except Exception:
        pass

    # Snapshot all docket entry numbers/dates NOW, before navigating into any
    # of them — same reasoning as the expediente snapshot: the list disappears
    # from the DOM once we click into an entry.
    docket_tiles = page.locator(".recourseDocketEntryTile__view")
    docket_count = docket_tiles.count()
    docket_numbers = []
    docket_dates = []
    for i in range(docket_count):
        try:
            num = docket_tiles.nth(i).locator(".recourseDocketEntryTile__docketNumber").inner_text(timeout=2000).strip()
        except Exception:
            num = str(i + 1)
        docket_numbers.append(num)

        try:
            tile = docket_tiles.nth(i)
            day   = tile.locator(".dateBlock__day").first.inner_text(timeout=1000).strip()
            mon   = tile.locator(".dateBlock__month").first.inner_text(timeout=1000).strip().lower()
            yr2   = tile.locator(".dateBlock__year").first.inner_text(timeout=1000).strip().lstrip("-")
            month = MESES.get(mon, "")
            docket_dates.append(f"20{yr2}-{month}-{day.zfill(2)}" if month and yr2 else "")
        except Exception:
            docket_dates.append("")

    print(f"  Found {docket_count} docket entries: {docket_numbers}")

    for i, docket_number in enumerate(docket_numbers):
        try:
            _process_recourse_docket_entry(page, i, case_number, docket_number, docket_dates[i],
                                           captured_pdf_data, session_blob_srcs)
        except Exception as e:
            print(f"  Error on docket entry {docket_number}: {e}")
            # Attempt to recover to the docket list so remaining entries can
            # still be processed.
            page.go_back()
            page.wait_for_timeout(3000)


def _process_case(page, case_idx, case_number, landing_url, captured_pdf_urls, captured_pdf_data,
                  tile_selector=".courtNotificationsBox__tile", label="Notification",
                  session_blob_srcs=None):
    """
    Level 2: open one case, process its expedientes, then return to Level 1.

    Expediente numbers are snapshotted before any navigation because the tile
    list disappears from the DOM once we drill into an expediente.  Up to 12
    expedientes are processed per case (configured in the loop below).

    case_idx      — zero-based tile index; re-queried after each navigation
                    because the panel may still be rendering on return.
    case_number   — extracted before navigation so it stays valid after DOM refresh.
    tile_selector — differs between the top-panel and bottom-panel tiles.
    label         — used in log output ("Notification" or "Case").
    """
    # Find the tile for this case by its case number text rather than by the
    # snapshot index.  SUMAC may reorder or remove notification tiles during a
    # session (e.g. marking them as "read"), which causes index drift and makes
    # tiles.nth(case_idx) click the wrong case — leading to the same content
    # being saved under multiple different case number filenames.
    tiles = page.locator(tile_selector)
    target_tile = None
    for j in range(tiles.count()):
        try:
            cn_loc = tiles.nth(j).locator(
                ".notificationTile__caseNumber, .notificationRecourseTile__recourseNumber"
            ).first
            if cn_loc.count() > 0 and cn_loc.inner_text(timeout=1000).strip() == case_number:
                target_tile = tiles.nth(j)
                break
        except Exception:
            pass
    if target_tile is None:
        # Fall back to index if case number text was not found in any tile.
        if case_idx >= tiles.count():
            print(f"  {label} tile for {case_number} not found, skipping.")
            return
        print(f"  Warning: '{case_number}' not found in tile text — using snapshot index {case_idx}.")
        target_tile = tiles.nth(case_idx)

    try:
        target_tile.wait_for(state="visible", timeout=8000)
    except Exception:
        print(f"  {label} tile for {case_number} not visible, skipping.")
        return

    print(f"\n=== {label} {case_idx + 1}: {case_number} ===")
    target_tile.click()
    page.wait_for_timeout(4000)  # Give the SPA time to load case detail content

    # Wait for VISIBLE expediente tiles — explicitly requiring state="visible"
    # prevents a false-pass on hidden tiles that SUMAC's SPA may leave in the
    # DOM from the previous case while the new case is still loading.
    try:
        page.wait_for_selector(".caseEntryTile__simpleView", state="visible", timeout=5000)
    except Exception:
        # Tribunal Apelativo ("TA...") cases use a completely different page
        # template — a flat docket list (.recourseDocketEntryTile__view inside
        # .COAEntriesContainer__scrollArea) instead of .caseEntryTile__simpleView
        # expediente tiles with Documento/Notificación tabs. Detect that case so
        # the log is accurate instead of misreporting "no expediente tiles"
        # when the page in fact has content we just don't parse yet.
        if page.locator(".recourseDocketEntryTile__view").count() > 0:
            print(f"  {case_number} is a Tribunal Apelativo (recourse) case — processing docket entries.")
            _process_recourse_case(page, case_number, captured_pdf_data, session_blob_srcs)
        else:
            print(f"  No expediente tiles found for {case_number}, skipping.")
        page.goto(landing_url)
        page.wait_for_timeout(3000)
        _wait_for_all_tiles(page)
        return

    # Read the authoritative case number from the case detail heading.
    # The heading title attribute has the format "{case_number} | {parties}".
    # Using this as ground truth prevents wrong labels when SUMAC navigation
    # lands on a different case than the one we clicked (linked tiles, etc.).
    try:
        heading_title = page.locator(".caseViewHeading__mainHeading").first.get_attribute("title", timeout=3000) or ""
        page_case_number = heading_title.split(" | ")[0].strip()
        if page_case_number and page_case_number != case_number:
            print(f"  Page shows case {page_case_number} (expected {case_number}) — using page value.")
            case_number = page_case_number
    except Exception:
        pass

    # Snapshot all expediente numbers NOW, before navigating into any of them.
    # Once we click into an expediente the tile list disappears from the DOM,
    # so we can't read numbers on-the-fly during iteration.
    exp_tiles = page.locator(".caseEntryTile__simpleView")
    exp_count = exp_tiles.count()
    exp_numbers = []
    exp_dates = []
    for i in range(exp_count):
        try:
            num = exp_tiles.nth(i).locator(".caseEntryTile__number").inner_text(timeout=2000).strip()
        except Exception:
            num = str(i + 1)
        exp_numbers.append(num)

        # Extract date from the date block on the left of each expediente tile.
        # Structure: dateBlock__day / dateBlock__month / dateBlock__year ("-26" → 2026)
        try:
            tile = exp_tiles.nth(i)
            day   = tile.locator(".dateBlock__day").first.inner_text(timeout=1000).strip()
            mon   = tile.locator(".dateBlock__month").first.inner_text(timeout=1000).strip().lower()
            yr2   = tile.locator(".dateBlock__year").first.inner_text(timeout=1000).strip().lstrip("-")
            month = MESES.get(mon, "")
            exp_dates.append(f"20{yr2}-{month}-{day.zfill(2)}" if month and yr2 else "")
        except Exception:
            exp_dates.append("")

    print(f"  Found {exp_count} expedientes: {exp_numbers}")

    for i, exp_number in enumerate(exp_numbers[:4]):
        try:
            _process_expediente(page, i, case_number, exp_number, exp_dates[i], captured_pdf_urls, captured_pdf_data,
                                session_blob_srcs)
        except Exception as e:
            print(f"  Error on expediente {exp_number}: {e}")
            # Attempt to recover to case detail so remaining expedientes can
            # still be processed.
            page.go_back()
            page.wait_for_timeout(3000)

    # Hard-navigate back to Level 1 (notifications landing page).
    page.goto(landing_url)
    page.wait_for_timeout(3000)
    _wait_for_all_tiles(page)


def scrape_all_pdfs(page):
    """
    Top-level scraping entry point (Level 1).

    Workflow:
      1. Ensure the output directory exists.
      2. Wait for the notifications landing page to render.
      3. Snapshot all notification tile case numbers before any navigation.
      4. Install a global network response listener that records every PDF URL
         the browser fetches — this powers Strategy 3 in _download_from_tab.
      5. Iterate over each notification, delegating to _process_case for Levels 2 & 3.
      6. Remove the response listener when done to avoid leaking it.
    """
    os.makedirs("sumac_documents", exist_ok=True)

    # Capture the landing URL now so _process_case can return here after each tile.
    landing_url = page.url

    print("Waiting for page panels to render...")
    page.wait_for_timeout(5000)
    _wait_for_all_tiles(page)

    # ── Snapshot both panels before any navigation ────────────────────────────

    # ── Bottom-left "Notificaciones Entre Partes" panel ──────────────────────
    # This panel has two tile classes:
    #   .partiesNotificationsBox__tile — the main between-parties notifications (~42)
    #   .courtNotificationsBox__tile   — recourse notifications inside this panel (~7)
    # We capture both with a combined selector.
    _BOTTOM_TILE_SEL = (
        ".home__bottomLeftPanel .partiesNotificationsBox__tile, "
        ".home__bottomLeftPanel .courtNotificationsBox__tile"
    )
    bottom_tiles = page.locator(_BOTTOM_TILE_SEL)
    bottom_count = bottom_tiles.count()
    bottom_case_numbers = []
    for i in range(bottom_count):
        try:
            num = bottom_tiles.nth(i).locator(
                ".notificationTile__caseNumber, .notificationRecourseTile__recourseNumber"
            ).first.inner_text(timeout=2000).strip()
        except Exception:
            num = f"miscase{i:03d}"
        bottom_case_numbers.append(num)
    print(f"Bottom panel: {bottom_count} cases: {bottom_case_numbers}")

    # ── Top "Notificaciones del Tribunal" panel ───────────────────────────────
    # Scoped directly to .home__topLeftPanel — no subtraction needed.
    _TOP_TILE_SEL = ".home__topLeftPanel .courtNotificationsBox__tile"
    notif_tiles = page.locator(_TOP_TILE_SEL)
    top_tile_count = notif_tiles.count()
    case_numbers = []
    for i in range(top_tile_count):
        try:
            num = notif_tiles.nth(i).locator(
                ".notificationTile__caseNumber, .notificationRecourseTile__recourseNumber"
            ).first.inner_text(timeout=2000).strip()
        except Exception:
            num = f"notif{i:03d}"
        case_numbers.append(num)
    print(f"Top panel: {top_tile_count} notifications: {case_numbers}")

    captured_pdf_urls = []
    captured_pdf_data = {}  # url → bytes captured at response time
    # Accumulates every blob: URL seen across ALL cases and expedientes this run.
    # Prevents the SPA's in-memory blobs from a previous case bleeding into a
    # later case's downloads when session_blob_srcs is passed to _process_expediente.
    session_blob_srcs: set = set()

    def on_response(response):
        """Intercept every HTTP response and record URLs that look like PDFs.

        Also captures the response body immediately so Strategy 3 never needs to
        make a second network request — critical for one-time-token PDF URLs.
        """
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        url = response.url.lower()
        if url.startswith("chrome-extension://"):
            return
        if "pdf" in ct.lower() or url.endswith(".pdf") or "pdf" in url:
            print(f"  [network] PDF detected: {response.url}")
            captured_pdf_urls.append(response.url)
            try:
                data = response.body()
                if data and data[:4] == b"%PDF":
                    captured_pdf_data[response.url] = data
            except Exception:
                pass  # URL still recorded; urllib/blob fallback will handle body

    page.on("response", on_response)

    processed_cases = set()

    # ── Process top "Notificaciones del Tribunal" panel first ────────────────
    print(f"\n{'='*60}")
    print(f"STARTING TOP PANEL  ({len(case_numbers)} notifications)")
    print(f"{'='*60}")
    for i, case_number in enumerate(case_numbers):
        if case_number in processed_cases:
            print(f"  Skipping {case_number} (already processed this run).")
            continue
        try:
            _process_case(
                page, i, case_number, landing_url,
                captured_pdf_urls, captured_pdf_data,
                tile_selector=_TOP_TILE_SEL,
                label="Notification",
                session_blob_srcs=session_blob_srcs,
            )
            processed_cases.add(case_number)
        except Exception as e:
            print(f"Error on notification {case_number}: {e}")
            page.goto(landing_url)
            page.wait_for_timeout(5000)

    # ── Process bottom-left "Notificaciones Entre Partes" panel second ────────
    print(f"\n{'='*60}")
    print(f"STARTING BOTTOM PANEL  ({bottom_count} cases)")
    print(f"{'='*60}")
    for i, case_number in enumerate(bottom_case_numbers):
        if case_number in processed_cases:
            print(f"  Skipping {case_number} (already processed).")
            continue
        try:
            _process_case(
                page, i, case_number, landing_url,
                captured_pdf_urls, captured_pdf_data,
                tile_selector=_BOTTOM_TILE_SEL,
                label="Case",
                session_blob_srcs=session_blob_srcs,
            )
            processed_cases.add(case_number)
        except Exception as e:
            print(f"Error on 'Notificaciones Entre Partes' {case_number}: {e}")
            page.goto(landing_url)
            page.wait_for_timeout(5000)

    page.remove_listener("response", on_response)
    print("\nAll panels processed.")

    _cleanup_old_pdfs()


# Holds the active browser instance so stop() can close it from outside.
_active_browser = None


def stop():
    """Close the active browser, causing run() to exit immediately."""
    global _active_browser
    if _active_browser:
        try:
            _active_browser.close()
        except Exception:
            pass


def run():
    """
    Main entry point called by the Flask /login route.

    Opens a visible (non-headless) Chromium window so the user can watch the
    automation in action, logs into SUMAC with the stored credentials, then
    kicks off the full PDF scraping session.

    Returns a human-readable status string that the Flask route forwards to the
    browser as JSON.  Raises on unrecoverable errors so Flask can return a 500.
    """
    global _active_browser
    username, password = read_credentials()
    current_url = ""

    # When frozen by PyInstaller, find chrome.exe next to the exe so we
    # never rely on Playwright's internal browser discovery (which points
    # to the temp extraction folder and ignores PLAYWRIGHT_BROWSERS_PATH).
    _chrome_exe = None
    if getattr(sys, 'frozen', False):
        _ms_playwright = Path(sys.executable).parent / "ms-playwright"
        for _d in sorted(_ms_playwright.glob("chromium-*"), reverse=True):
            _c = _d / "chrome-win64" / "chrome.exe"
            if _c.exists():
                _chrome_exe = str(_c)
                break
        if _chrome_exe:
            print(f"Using Chromium: {_chrome_exe}")
        else:
            print("⚠️  chrome.exe not found next to exe — using Playwright default")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                executable_path=_chrome_exe,  # None = auto-detect (non-frozen)
            )
            _active_browser = browser
            page = browser.new_page()
            page.goto(SUMAC_URL)

            page.fill("input[name='username'], input[type='text'], #username", username)
            page.fill("input[name='password'], input[type='password'], #password", password)

            print("Clicking 'Acceder'...")
            login_button = page.get_by_role("button", name="Acceder", exact=True)
            login_button.wait_for(state="visible", timeout=5000)
            login_button.click()

            print("Login clicked. Checking for successful entry...")
            time.sleep(20)

            page.wait_for_load_state("networkidle")

            current_url = page.url
            print(f"Landing URL after login: {current_url}")

            print("\nReconciling UNKNOWN Dropbox files before downloading...")
            import dropbox_sync2
            dropbox_sync2.move_files_from_UNKNOWN_to_dropbox_subfolders()

            try:
                scrape_all_pdfs(page)
            except Exception as e:
                # TargetClosedError is raised when stop() closes the browser mid-run.
                print(f"\n[Scraping ended early: {e.__class__.__name__}]")
    except Exception as e:
        # Catch errors during playwright context cleanup (e.g. greenlet/thread errors
        # when stop() closes the browser from the GUI thread).
        print(f"\n[Browser session ended: {e.__class__.__name__}]")
    finally:
        _active_browser = None

    if "signIn" in current_url:
        return "Login may have failed — still on sign-in page."

    print("\nSyncing downloaded files to Dropbox…")
    import dropbox_sync2
    dropbox_sync2.copy_files_to_dropbox_subfolders()

    return f"Login successful. Redirected to: {current_url}"
