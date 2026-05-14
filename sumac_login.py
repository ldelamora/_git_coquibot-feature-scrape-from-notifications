# sumac_login.py — Playwright automation for logging into SUMAC and scraping PDFs.
#
# Navigation follows a 3-level hierarchy mirrored by SUMAC's SPA (Single-Page App):
#   Level 1  →  "My Cases" list  (.caseTile__view tiles)
#   Level 2  →  Case detail view  (.caseEntryTile__simpleView expediente tiles)
#   Level 3  →  Expediente detail  (Documento / Notificación tabs with downloadable PDFs,
#               plus optional "Anejo" attachment pills in div.attachmentsPillbox)
#
# Because SUMAC is a SPA, going "back" between levels uses page.go_back() or
# page.goto() to force a full re-navigation rather than relying on DOM re-renders.

from playwright.sync_api import sync_playwright
from pathlib import Path
import os
import re
import urllib.request
import time

# Entry point for the sign-in form.
SUMAC_URL = "https://tribunalelectronico.ramajudicial.pr/sumac2018/signIn.html"

# Plain-text credentials file (gitignored). Line 1 = username, line 2 = password.
CREDENTIALS_FILE = "sumac.txt"

# Tab names inside each expediente that may contain downloadable PDFs.
TABS_TO_CHECK = ["Documento", "Notificación"]


def read_credentials():
    """Read username and password from the two-line credentials file."""
    with open(CREDENTIALS_FILE, "r") as f:
        lines = [line.strip() for line in f.readlines()]
    if len(lines) < 2:
        raise ValueError("sumac.txt must have username on line 1 and password on line 2")
    return lines[0], lines[1]


def _cookie_header(page):
    """
    Build a Cookie header string from the current browser session.
    Needed when downloading files via urllib (outside of Playwright) so that
    the server still recognises the authenticated session.
    """
    cookies = page.context.cookies()
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _save_pdf_from_url(page, url, save_path):
    """
    Download a PDF and save it to `save_path`.  Returns True on success.

    Handles two URL schemes:
    - blob:  — in-memory object URLs created by the browser (e.g. PDF.js blobs).
              These cannot be fetched by urllib; we use page.evaluate() to read
              the bytes from inside the browser context and return them as base64.
    - http/https — fetched via urllib with the session cookies forwarded so the
                   server recognises the authenticated session.
    """
    import base64

    if url.startswith("blob:"):
        try:
            # Read the blob from inside the browser, encode as base64, decode in Python.
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

    try:
        req = urllib.request.Request(url, headers={"Cookie": _cookie_header(page)})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                with open(save_path, "wb") as f:
                    f.write(resp.read())
                return True
    except Exception as e:
        print(f"    urllib download failed for {url}: {e}")
    return False


def _already_downloaded(prefix):
    """Return True if sumac_documents already contains a file whose name starts with prefix."""
    dest = Path("sumac_documents")
    if not dest.exists():
        return False
    return any(f.name.startswith(prefix) for f in dest.iterdir() if f.is_file())


MESES = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
    # 3-letter abbreviations used in the Level 2 date block
    "ene": "01", "feb": "02", "mar": "03", "abr": "04",
    "may": "05", "jun": "06", "jul": "07", "ago": "08",
    "sep": "09", "oct": "10", "nov": "11", "dic": "12",
}


def _parse_date_es(text):
    """Parse a Spanish date string 'dd de mes de yyyy' → 'yyyy-mm-dd', or '' on failure."""
    m = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", text, re.IGNORECASE)
    if m:
        month = MESES.get(m.group(2).lower(), "")
        if month:
            return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"
    return ""


def _download_from_tab(page, tab_name, filename_prefix, captured_pdf_urls):
    """
    Click a tab inside an expediente detail view and attempt to save any PDF
    it contains.  Three strategies are tried in order of preference:
      1. Dedicated download button rendered by SUMAC (.caseEntriesView__downloadButton)
      2. Any generic anchor or element that triggers a Playwright download event
      3. PDF URLs captured passively from network responses (see on_response in
         scrape_all_pdfs) — used when the PDF is rendered inline via PDF.js and
         never triggers a normal browser download.
    Returns True as soon as one PDF is saved, False if nothing was found.
    """
    # Locate the tab button — SUMAC sometimes uses a title attribute, sometimes
    # just inner text, so we fall back to text-based filtering.
    tab = page.locator(f"button[title='{tab_name}']")
    if tab.count() == 0:
        tab = page.locator("button").filter(has_text=tab_name)
    if tab.count() == 0:
        print(f"    Tab '{tab_name}' not found.")
        return False

    # Skip the network round-trip if this tab's PDF was already saved in a prior run.
    # The prefix {filename_prefix}_{tab_name}_ is unique enough (encodes date,
    # expediente number, case code, and tab name) to avoid false positives.
    if _already_downloaded(f"{filename_prefix}_{tab_name}_"):
        print(f"    [{tab_name}] Already downloaded, skipping.")
        return True

    # Clear the shared URL buffer before clicking so we only capture URLs that
    # result from this specific tab activation.
    captured_pdf_urls.clear()
    tab.first.click()
    page.wait_for_timeout(4000)  # Let the tab content load before inspecting DOM

    # Read the document title shown in the tab header (h1 inside the document
    # header container).  Prefer the title attribute; fall back to inner text.
    doc_title = ""
    try:
        h1 = page.locator(".caseEntryDocumentContainer__documentHeader h1").first
        doc_title = (h1.get_attribute("title") or h1.inner_text(timeout=1000) or "").strip()
        # Sanitize: remove characters illegal in filenames, collapse whitespace.
        doc_title = re.sub(r'[\\/:*?"<>|.]+', '', doc_title).strip()
        doc_title = re.sub(r'\s+', ' ', doc_title)[:50]
     
    except Exception:
        doc_title = ""

    title_part = f" - {doc_title}" if doc_title else ""

    # Strategy 1: dedicated download button (.caseEntriesView__downloadButton)
    dl_btn = page.locator(".caseEntriesView__downloadButton")
    if dl_btn.count() > 0:
        for j in range(dl_btn.count()):
            try:
                print(f"    [{tab_name}] Clicking download button {j+1}...")
                # expect_download() intercepts the file-download dialog that
                # Playwright would otherwise handle silently.
                with page.expect_download(timeout=3000) as dl_info:
                    dl_btn.nth(j).click()
                dl = dl_info.value
                #fname = f"{filename_prefix}_{tab_name}_{j+1}{title_part}_{dl.suggested_filename or 'document.pdf'}"
                fname = f"{filename_prefix}_{tab_name}_{j+1}{title_part}.pdf"
                    
                save_path = os.path.join("sumac_documents", fname)
                dl.save_as(save_path)
                print(f"    Saved: {save_path}")
                return True
            except Exception as e:
                print(f"    Download button failed: {e}")

    # Strategy 2: any other download-triggering links/buttons
    for selector in ["a[download]", "a[href*='.pdf']"]:
        elems = page.locator(selector)
        if elems.count() > 0:
            for j in range(elems.count()):
                try:
                    with page.expect_download(timeout=1000) as dl_info:
                        elems.nth(j).click()
                    dl = dl_info.value
                    #fname = f"{filename_prefix}_{tab_name}_{j+1}{title_part}_{dl.suggested_filename or 'document.pdf'}"
                    fname = f"{filename_prefix}_{tab_name}_{j+1}{title_part}.pdf"
                    save_path = os.path.join("sumac_documents", fname)
                    dl.save_as(save_path)
                    print(f"    Saved: {save_path}")
                    return True
                except Exception:
                    pass

    # Strategy 3: PDF URL intercepted from network traffic.
    # If SUMAC loads the PDF inline (e.g. via PDF.js), no download event fires.
    # Instead we fall back to the URLs collected by the response listener and
    # fetch them manually using urllib with the session cookies.
    if captured_pdf_urls:
        for j, url in enumerate(list(captured_pdf_urls)):
            fname = f"{filename_prefix}_{tab_name}_{j+1}{title_part}.pdf"
            save_path = os.path.join("sumac_documents", fname)
            print(f"    [{tab_name}] Saving intercepted PDF: {url}")
            if _save_pdf_from_url(page, url, save_path):
                print(f"    Saved: {save_path}")
                return True

    print(f"    [{tab_name}] No PDF found.")
    return False


def _download_anejo_attachments(page, filename_prefix, captured_pdf_urls):
    """
    Download all PDFs attached as "Anejo" pills inside an expediente detail view.

    SUMAC renders these inside:
        div.elementContainer.pillBox.caseEntryDocumentContainer.attachmentsPillbox

    Each child element of that container is a clickable pill.  Clicking a pill
    loads a PDF — either as a browser download or inline via PDF.js.  We handle
    both cases using the same network-interception strategy used elsewhere.

    Filenames follow the pattern: <filename_prefix>_anejo_<n>.pdf
    """
    # BEM class confirmed from DOM inspection.
    container = page.locator("div.caseEntryDocumentContainer__attachmentsPillbox")
    if container.count() == 0:
        # No Anejo section present on this expediente — nothing to do.
        return

    # Each attachment is rendered as a .caseEntryDocumentContainer__attachmentTile.
    pills = container.locator(".caseEntryDocumentContainer__attachmentTile")
    pill_count = pills.count()
    if pill_count == 0:
        print(f"    [Anejo] Container found but no attachment tiles inside.")
        return

    print(f"    [Anejo] Found {pill_count} attachment(s).")

    # Iterate last-to-first: clicking pill 1 first when the viewer already shows
    # the Notificación PDF causes it to display pill 1's cached blob without
    # firing a new network request, so we can't detect it.  Going in reverse
    # means by the time we reach pill 1, the viewer holds a different pill's
    # content, forcing a real reload and a fresh blob URL we can capture.
    for j in range(pill_count - 1, -1, -1):
        # Skip if this attachment index was already saved in a prior run.
        if _already_downloaded(f"{filename_prefix}_anejo_{j + 1}"):
            print(f"    [Anejo] Attachment {j + 1} already downloaded, skipping.")
            continue

        # Read the pill label BEFORE clicking — it may change or disappear after.
        try:
            raw_label = pills.nth(j).inner_text(timeout=1000).strip()
        except Exception:
            raw_label = ""
        # Sanitize for use in a filename: collapse whitespace, remove illegal chars.
        pill_label = re.sub(r'[\\/:*?"<>|]+', '', raw_label).strip()
        pill_label = re.sub(r'\s+', ' ', pill_label)

        print(f"    [Anejo] Clicking attachment {j + 1}/{pill_count}: '{pill_label}'...")

        # Snapshot BEFORE clicking so we can detect what the single click produces.
        urls_before = list(captured_pdf_urls)

        try:
            # Strategy A: click triggers a browser download event.
            with page.expect_download(timeout=1500) as dl_info:
                pills.nth(j).click()
            dl = dl_info.value
            base = dl.suggested_filename or 'attachment.pdf'
            label_part = f" - {pill_label}" if pill_label else ""
            fname = f"{filename_prefix}_anejo_{j + 1}{label_part}_{base}"
            save_path = os.path.join("sumac_documents", fname)
            dl.save_as(save_path)
            print(f"    [Anejo] Saved: {save_path}")
            continue
        except Exception:
            # No download event — Strategy A's click still happened; the PDF
            # likely loaded inline as a blob. Fall through to Strategy B.
            pass

        # Strategy B: wait for the blob that the Strategy A click produced,
        # then save it.  No second click — the pill was already clicked above.
        page.wait_for_timeout(1500)
        new_urls = [u for u in captured_pdf_urls if u not in urls_before]
        new_blobs = [u for u in new_urls if u.startswith("blob:")]
        url = (new_blobs or new_urls or [None])[-1]

        if url:
            label_part = f" - {pill_label}" if pill_label else ""
            fname = f"{filename_prefix}_anejo_{j + 1}{label_part}.pdf"
            save_path = os.path.join("sumac_documents", fname)
            print(f"    [Anejo] Saving intercepted PDF: {url}")
            if _save_pdf_from_url(page, url, save_path):
                print(f"    [Anejo] Saved: {save_path}")
                continue

        print(f"    [Anejo] Could not save attachment {j + 1}.")


def _process_expediente(page, exp_idx, case_number, exp_number, exp_date, captured_pdf_urls):
    """
    Level 3: click an expediente tile, check Documento/Notificación tabs,
    then go back to the case detail view (Level 2).

    exp_idx      — zero-based position of the tile in the current DOM list.
    exp_number   — human-readable expediente number (used in saved filenames).
    captured_pdf_urls — shared list populated by the network response listener;
                        passed into each tab handler so Strategy 3 can use it.
    """
    # Re-query tiles here because navigating back from a previous expediente
    # can trigger a DOM refresh, potentially invalidating stale locators.
    tiles = page.locator(".caseEntryTile__simpleView")
    if exp_idx >= tiles.count():
        print(f"  Expediente tile {exp_idx} no longer in DOM, skipping.")
        return

    print(f"  Expediente {exp_idx + 1}: #{exp_number}")
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

    # Build the filename prefix: date first so files sort chronologically.
    date_prefix = f"{exp_date}_" if exp_date else ""
    filename_prefix = f"{date_prefix}[{exp_number}]_{case_number}"

    # Check for Anejo (attachment) pills BEFORE clicking any tab, because the
    # pillbox may only be visible in the default expediente view.
    _download_anejo_attachments(page, filename_prefix, captured_pdf_urls)

    for tab_name in TABS_TO_CHECK:
        _download_from_tab(page, tab_name, filename_prefix, captured_pdf_urls)

    # Return to case detail (Level 2) using browser history.
    # wait_for_selector ensures the expediente list is ready before the caller
    # tries to access the next tile index.
    page.go_back()
    page.wait_for_selector(".caseEntryTile__simpleView", timeout=5000)


def _process_case(page, case_idx, case_number, landing_url, captured_pdf_urls):
    """
    Level 2: click a case tile to open its detail view, iterate over every
    expediente inside it, then navigate back to the cases list (Level 1).

    case_idx    — zero-based index into the current case tile list.
    case_number — human-readable case number extracted before any navigation,
                  so it remains valid even after the DOM is refreshed.
    """
    # Re-query notification tiles in case the DOM was rebuilt after the previous
    # navigation cycle.
    tiles = page.locator(".courtNotificationsBox__tile")
    if case_idx >= tiles.count():
        print(f"Notification tile {case_idx} no longer in DOM, skipping.")
        return

    print(f"\n=== Notification {case_idx + 1}: {case_number} ===")
    tiles.nth(case_idx).click()
    page.wait_for_timeout(4000)  # Give the SPA time to load case detail content

    # If the case has no expediente tiles (e.g. empty case, permission error),
    # bail early and reset back to Level 1 so the loop can continue.
    try:
        page.wait_for_selector(".caseEntryTile__simpleView", timeout=3000)
    except Exception:
        print(f"  No expediente tiles found for {case_number}, skipping.")
        page.goto(landing_url)
        page.wait_for_timeout(3000)
        page.wait_for_selector(".courtNotificationsBox__tile", timeout=3000)
        return

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

    for i, exp_number in enumerate(exp_numbers[:3]):
        try:
            _process_expediente(page, i, case_number, exp_number, exp_dates[i], captured_pdf_urls)
        except Exception as e:
            print(f"  Error on expediente {exp_number}: {e}")
            # Attempt to recover to case detail so remaining expedientes can
            # still be processed.
            page.go_back()
            page.wait_for_timeout(3000)

    # Hard-navigate back to Level 1 (notifications landing page).
    page.goto(landing_url)
    page.wait_for_timeout(3000)
    page.wait_for_selector(".courtNotificationsBox__tile", timeout=5000)


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

    print("Waiting for notifications to render...")
    page.wait_for_timeout(5000)

    try:
        page.wait_for_selector(".courtNotificationsBox__tile", timeout=5000)
    except Exception as e:
        print(f"Notification tiles never appeared: {e}")
        return

    # Snapshot case numbers before entering the navigation loop.
    # Notification tiles use two different BEM variants depending on the document type.
    tiles = page.locator(".courtNotificationsBox__tile")
    tile_count = tiles.count()
    case_numbers = []
    for i in range(tile_count):
        try:
            num = tiles.nth(i).locator(
                ".notificationTile__caseNumber, .notificationRecourseTile__recourseNumber"
            ).first.inner_text(timeout=2000).strip()
        except Exception:
            num = f"notif{i:03d}"
        case_numbers.append(num)

    print(f"Found {tile_count} notifications: {case_numbers}")

    captured_pdf_urls = []

    def on_response(response):
        """Intercept every HTTP response and record URLs that look like PDFs."""
        ct = response.headers.get("content-type", "")
        url = response.url.lower()
        if url.startswith("chrome-extension://"):
            return
        if "pdf" in ct.lower() or url.endswith(".pdf") or "pdf" in url:
            print(f"  [network] PDF detected: {response.url}")
            captured_pdf_urls.append(response.url)

    page.on("response", on_response)

    processed_cases = set()

    for i, case_number in enumerate(case_numbers):
        if case_number in processed_cases:
            print(f"  Skipping {case_number} (already processed this run).")
            continue
        try:
            _process_case(page, i, case_number, landing_url, captured_pdf_urls)
            processed_cases.add(case_number)
        except Exception as e:
            print(f"Error on notification {case_number}: {e}")
            page.goto(landing_url)
            page.wait_for_timeout(5000)

    page.remove_listener("response", on_response)
    print("\nAll notifications processed.")


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

    with sync_playwright() as p:
        # headless=False keeps the browser window open so behaviour is visible
        # and easier to debug when something goes wrong.
        browser = p.chromium.launch(headless=False)
        _active_browser = browser
        page = browser.new_page()
        page.goto(SUMAC_URL)

        # Fill the login form using a multi-selector string so the locator works
        # regardless of which attribute SUMAC uses to identify its inputs.
        page.fill("input[name='username'], input[type='text'], #username", username)
        page.fill("input[name='password'], input[type='password'], #password", password)

        print("Clicking 'Acceder'...")
        login_button = page.get_by_role("button", name="Acceder", exact=True)
        login_button.wait_for(state="visible", timeout=5000)
        login_button.click()

        print("Login clicked. Checking for successful entry...")
        time.sleep(20)

        # networkidle means no in-flight XHR requests for ≥500 ms — a good
        # signal that the post-login redirect has fully settled.
        page.wait_for_load_state("networkidle")
        
        # Stay on the import time
        # post-login landing page (notifications) — scraping starts here.
        current_url = page.url
        print(f"Landing URL after login: {current_url}")

        try:
            scrape_all_pdfs(page)
        except Exception as e:
            # TargetClosedError is raised when stop() closes the browser mid-run.
            # Any files already downloaded are still synced to Dropbox below.
            print(f"\n[Scraping ended early: {e.__class__.__name__}]")

    _active_browser = None

    # A simple heuristic: if we're still on the sign-in page the credentials
    # were likely rejected.
    if "signIn" in current_url:
        return "Login may have failed — still on sign-in page."

    print("\nSyncing downloaded files to Dropbox…")
    import dropbox_sync2
    dropbox_sync2.copy_files_to_dropbox_subfolders()

    return f"Login successful. Redirected to: {current_url}"
