"""
dropbox_sync2.py — Sync SUMAC court documents to Dropbox.

Reads files from ./sumac_documents (or a custom path).  For each file it:
  1. Extracts the 13-char case code embedded in the filename
     (pattern: 2 letters + 4 digits + 2 letters + 5 digits, e.g. FA2025CV00220).
  2. Searches all subfolders under the Dropbox destination for a folder whose
     name contains that case code (e.g. "2079.01 - FA2025CV00220 (Injuction PCOC)").
  3. Copies the file into a SUMAC/ subfolder inside the match, creating it if absent.
     If no matching folder is found, the file goes into a SUMAC/ subfolder under
     Coquibot/UNKNOWN/ (created automatically).

Files that already exist at the destination are skipped (no overwrite).

Usage examples:
  python dropbox_sync2.py                                           # default paths
  python dropbox_sync2.py --source "C:\\docs" --dest "D:\\Dropbox"  # custom paths
  python dropbox_sync2.py --preview                                 # dry-run preview
  python dropbox_sync2.py --source "C:\\docs" --preview            # preview + custom source
"""

import csv
import datetime
import re
import unicodedata
import sys
import shutil
import argparse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

if getattr(sys, 'frozen', False):
    _SCRIPT_DIR = Path(sys.executable).parent
else:
    _SCRIPT_DIR = Path(__file__).parent

# Matches SUMAC case codes:
#   - FA2025CV00220  (2 letters, 4 digits, 2 letters, 5 digits)
#   - AP-202600232   (Tribunal Apelativo style: 2 letters, hyphen, 9 digits)
CASE_CODE_RE = re.compile(r'[A-Z]{2}\d{4}[A-Z]{2}\d{5}|[A-Z]{2}-\d{9}')

# Email configuration — credentials are read from email.txt (gitignored).
# Line 1: sender address  (coquibot.system@gmail.com)
# Line 2: Gmail App Password  (generate at Google Account → Security → App Passwords)
# Line 3: recipient address
_EMAIL_SUBJECT   = "New files from SUMAC downloaded"
_SMTP_HOST       = "smtp.gmail.com"
_SMTP_PORT       = 587
_DROPBOX_DEFAULT = Path(r"C:\Users\luisd\Dropbox\Coquibot")
_DROPBOX_LOG     = _SCRIPT_DIR / "dropboxLog.txt"
_CASES_EMAILS = _SCRIPT_DIR / "casesEmails.csv"
_CLIENT_BACKLOG = _SCRIPT_DIR / "clientEmailsBacklog.csv"
_CLIENT_BACKLOG_HEADER = ["Date recorded", "Date to send email", "Case number",
                          "Expediente number", "Email", "Name", "Sent date"]
_CLIENT_WAIT_DAYS_DEFAULT = 1
_CLIENT_BACKLOG_KEEP_DAYS = 183  # sent lines whose send date is older than ~6 months are dropped

_BUZON_FOLDER_NAME = "_Buzon_de_Mindie"  # created directly under the Dropbox destination

_BATCH_SIZE = 10 # max attachments per case notification email

_CASE_EMAIL_SUBJECT = "Timothée-Vega Law: Nuevos documentos relacionados con su caso"
_CASE_EMAIL_BODY    = """\
Estimado/a {name} ,

Se aneja copia de nuevos documentos relacionados con su caso, para su conocimiento y revisión.

De tener alguna duda o pregunta, no dude en comunicarse con nuestra oficina.

Cordialmente,

Timothée Vega Law LLC
https://www.timotheelaw.com
PO Box 29149, San Juan PR 00929-0194
Phone: (787) 764-5517 | E-mail: despachotimothee@gmail.com


"""


def _read_config_lines() -> list[str]:
    """config.txt: line 1 = Dropbox destination folder, line 2 = wait days for clients."""
    config_path = _SCRIPT_DIR / "config.txt"
    if not config_path.exists():
        return []
    return [l.strip() for l in config_path.read_text(encoding="utf-8").splitlines()]


def _read_dropbox_dest() -> Path:
    """Read the Dropbox destination folder from config.txt, falling back to the default."""
    lines = _read_config_lines()
    if lines and lines[0]:
        return Path(lines[0])
    return _DROPBOX_DEFAULT


def _read_client_wait_days() -> int:
    """Read "Wait days for clients" from config.txt line 2, falling back to the default."""
    lines = _read_config_lines()
    try:
        return max(0, int(lines[1]))
    except (IndexError, ValueError):
        return _CLIENT_WAIT_DAYS_DEFAULT


def _write_dropbox_log(new_files: list[str]) -> None:
    """Append a dated batch entry to dropboxLog.txt for every file sent to Dropbox."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "",
        timestamp,
        "",
    ] + new_files + [""]
    with open(_DROPBOX_LOG, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _read_email_config():
    """Read sender, app-password and recipient list from email.txt.

    Line 1: sender address
    Line 2: Gmail App Password
    Line 3+: recipient addresses (one per line)
    """
    config_path = _SCRIPT_DIR / "email.txt"
    with open(config_path, encoding="utf-8") as f:
        lines = [l.strip() for l in f.readlines()]
    if len(lines) < 3:
        raise ValueError("email.txt must have: line 1 = sender, line 2 = app password, line 3+ = recipients")
    sender     = lines[0]
    password   = lines[1]
    recipients = [l for l in lines[2:] if l]
    return sender, password, recipients


def _send_email(new_files: list[str]) -> None:
    """Send a notification email listing the newly copied files."""
    email_from, app_password, recipients = _read_email_config()
    if not recipients:
        print("⚠️  No recipients configured in email.txt — skipping notification.")
        return

    body = "The following new files were transferred to Dropbox:\n\n"
    body += "\n".join(f"  • {f}" for f in new_files)
    body += f"\n\nTotal: {len(new_files)} file(s)."

    msg = MIMEMultipart()
    msg["From"]    = email_from
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = _EMAIL_SUBJECT
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
            server.starttls()
            server.login(email_from, app_password)
            server.sendmail(email_from, recipients, msg.as_string())
        print(f"📧 Email notification sent to: {', '.join(recipients)}")
    except Exception as e:
        print(f"❌ Failed to send email notification: {e}")


def _read_cases_emails(clients: bool = False) -> dict[str, list[tuple[str, str]]]:
    """Return {case_code: [(email, name), ...]} from casesEmails.csv.

    Col A = case code, col B = email, col C = recipient name (optional),
    col D = "C" for a client row (sent later via clientEmailsBacklog.csv).
    `clients` selects the client rows; otherwise only the non-client rows are returned.
    The same case code may appear on multiple rows; each row adds another recipient.
    """
    if not _CASES_EMAILS.exists():
        return {}
    mapping: dict[str, list[tuple[str, str]]] = {}
    with open(_CASES_EMAILS, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                is_client = len(row) >= 4 and row[3].strip().upper() == "C"
                if is_client != clients:
                    continue
                name = row[2].strip() if len(row) >= 3 else ""
                mapping.setdefault(row[0].strip(), []).append((row[1].strip(), name))
    # Header row filtered out — its column A won't match the 13-char case code pattern
    return {k: v for k, v in mapping.items() if CASE_CODE_RE.fullmatch(k)}


def _send_case_batches(server, sender: str, recipient: str, name: str, code: str,
                       fnames: list[str], log_tag: str = "") -> None:
    """Send `fnames` (from sumac_documents) to one recipient, `_BATCH_SIZE` files per email."""
    batches = [fnames[i:i + _BATCH_SIZE] for i in range(0, len(fnames), _BATCH_SIZE)]
    total_batches = len(batches)
    for batch_idx, batch in enumerate(batches, start=1):
        if total_batches > 1:
            subject = f"{batch_idx} de {total_batches}: {_CASE_EMAIL_SUBJECT}"
        else:
            subject = _CASE_EMAIL_SUBJECT
        body = _CASE_EMAIL_BODY.format(name=name)
        msg = MIMEMultipart()
        msg["From"]    = sender
        msg["To"]      = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for fname in batch:
            fpath = _SCRIPT_DIR / "sumac_documents" / fname
            if fpath.exists():
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fpath.read_bytes())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=fname)
                msg.attach(part)
        server.sendmail(sender, [recipient], msg.as_string())
        batch_label = f" ({batch_idx}/{total_batches})" if total_batches > 1 else ""
        print(f"📧 {log_tag}Case email{batch_label} → {recipient}  (case {code}, {len(batch)} file(s))")
        with open(_DROPBOX_LOG, "a", encoding="utf-8") as f:
            f.write(f"📧 {log_tag}Email sent to: {recipient} (case {code}){batch_label}\n")


def _send_case_emails(new_files: list[str]) -> None:
    """For each case code in new_files that appears in casesEmails.csv, send one email.

    Client rows (col D = "C") are skipped here — see _process_client_emails."""
    cases_map = _read_cases_emails()
    if not cases_map:
        return

    # Group new files by case code
    by_case: dict[str, list[str]] = {}
    for fname in new_files:
        code = get_case_code(fname)
        if code:
            by_case.setdefault(code, []).append(fname)

    # Keep only codes that have a recipient
    to_notify = {code: files for code, files in by_case.items() if code in cases_map}
    if not to_notify:
        return

    try:
        sender, password, _ = _read_email_config()
    except Exception as e:
        print(f"⚠️  Could not read email config for case notifications: {e}")
        return

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
            server.starttls()
            server.login(sender, password)
            for code, files in to_notify.items():
                for recipient, name in cases_map[code]:
                    _send_case_batches(server, sender, recipient, name, code, files)
    except Exception as e:
        print(f"❌ Failed to send case notification emails: {e}")


# ── emails_to_clients ─────────────────────────────────────────────────────────
# Client rows of casesEmails.csv (col D = "C") are not emailed right away.  Each
# new expediente is queued in clientEmailsBacklog.csv with a send date of
# today + "Wait days for clients"; every run sends the lines that are due.

_EXPEDIENTE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}_\[(\d+)\]')


def _read_client_backlog() -> list[dict[str, str]]:
    if not _CLIENT_BACKLOG.exists():
        return []
    with open(_CLIENT_BACKLOG, encoding="utf-8-sig", newline="") as f:
        return [
            {k: (row.get(k) or "").strip() for k in _CLIENT_BACKLOG_HEADER}
            for row in csv.DictReader(f)
        ]


def _write_client_backlog(rows: list[dict[str, str]]) -> None:
    with open(_CLIENT_BACKLOG, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CLIENT_BACKLOG_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def _expediente_files(code: str, exp: str) -> list[str]:
    """Files in sumac_documents for one expediente of a case, minus NOTIFICACIÓN AUTOMÁTICA.

    Matched on the "<date>_[<exp>]" prefix + case code rather than full names,
    since files may have been renamed (initials, referenced-number tags) since."""
    folder = _SCRIPT_DIR / "sumac_documents"
    if not folder.exists():
        return []
    files = []
    for f in folder.iterdir():
        m = _EXPEDIENTE_RE.match(f.name)
        if (f.is_file() and m and m.group(1) == exp and get_case_code(f.name) == code
                and not is_excluded_from_sync(f.name)):
            files.append(f.name)
    return sorted(files)


def _process_client_emails(new_files: list[str]) -> None:
    """Queue new expedientes for client recipients, then send backlog lines that are due."""
    clients_map = _read_cases_emails(clients=True)
    rows = _read_client_backlog()
    today = datetime.date.today()

    # 1. Queue: one line per (case, expediente, client email).
    if clients_map and new_files:
        send_on = (today + datetime.timedelta(days=_read_client_wait_days())).isoformat()
        queued = {(r["Case number"], r["Expediente number"], r["Email"].lower()) for r in rows}
        for fname in new_files:
            code = get_case_code(fname)
            m = _EXPEDIENTE_RE.match(fname)
            if not code or not m or code not in clients_map:
                continue
            for email, name in clients_map[code]:
                key = (code, m.group(1), email.lower())
                if key in queued:
                    continue
                queued.add(key)
                rows.append({
                    "Date recorded": today.isoformat(), "Date to send email": send_on,
                    "Case number": code, "Expediente number": m.group(1),
                    "Email": email, "Name": name, "Sent date": "",
                })
                print(f"🕒 Client email queued for {send_on} → {email}  (case {code}, exp {m.group(1)})")

    # 2. Send the lines that are due (send date today or earlier, not yet sent).
    def _date(s):
        try:
            return datetime.date.fromisoformat(s)
        except ValueError:
            return None

    due = [r for r in rows if not r["Sent date"] and _date(r["Date to send email"])
           and _date(r["Date to send email"]) <= today]
    if due:
        try:
            sender, password, _ = _read_email_config()
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
                server.starttls()
                server.login(sender, password)
                for r in due:
                    files = _expediente_files(r["Case number"], r["Expediente number"])
                    if not files:
                        print(f"⚠️  Client email not sent — no files found for case "
                              f"{r['Case number']}, exp {r['Expediente number']} (will retry next run)")
                        continue
                    _send_case_batches(server, sender, r["Email"], r["Name"],
                                       r["Case number"], files, log_tag="Client ")
                    r["Sent date"] = today.isoformat()
        except Exception as e:
            print(f"❌ Failed to send client emails: {e}")

    # 3. Drop sent lines whose send date is older than ~6 months.
    cutoff = today - datetime.timedelta(days=_CLIENT_BACKLOG_KEEP_DAYS)
    rows = [r for r in rows if not (r["Sent date"] and _date(r["Date to send email"])
                                    and _date(r["Date to send email"]) < cutoff)]
    _write_client_backlog(rows)


def is_excluded_from_sync(filename):
    """True for files that stay local and are never sent to Dropbox
    ("NOTIFICACIÓN AUTOMÁTICA" in the name; accent/case-insensitive)."""
    plain = unicodedata.normalize("NFD", filename).encode("ascii", "ignore").decode().lower()
    return "notificacion automatica" in plain


def get_case_code(filename):
    """Return the first SUMAC case code found in filename, or None."""
    match = CASE_CODE_RE.search(filename)
    return match.group(0) if match else None


def find_case_folder(root, case_code, _cache={}):
    """
    Search root (recursively) for a directory whose name contains case_code.

    Found folders are cached so repeated lookups for the same code only scan
    once. Misses are deliberately NOT cached: this function lives for the
    whole GUI process (which can run for days via the Scheduler), and a
    matching Dropbox folder may be created after an earlier miss — caching
    "not found" would keep files stuck in UNKNOWN even after the folder shows
    up, since a later call would return the stale None instead of rechecking.
    Returns the matching Path, or None if not found.
    """
    cache_key = (str(root), case_code)
    if cache_key in _cache:
        return _cache[cache_key]

    result = None
    for folder in Path(root).rglob('*'):
        if folder.is_dir() and case_code in folder.name:
            result = folder
            break

    if result is not None:
        _cache[cache_key] = result
    return result


def copy_files_to_dropbox_subfolders(source_folder=None, destination_folder=None):
    """
    Copy files from source_folder into the correct Dropbox case folders.

    Each file is routed to <case_folder>/SUMAC/ where <case_folder> is the
    Dropbox subfolder whose name contains the file's SUMAC case code.
    """

    # Set default paths if not provided
    if source_folder is None:
        source_folder = _SCRIPT_DIR / "sumac_documents"

    if destination_folder is None:
        destination_folder = _read_dropbox_dest()

    source_folder = Path(source_folder)
    destination_folder = Path(destination_folder)

    if not source_folder.exists():
        print(f"Error: Source folder '{source_folder}' does not exist.")
        return

    if not destination_folder.exists():
        print(f"Error: Destination folder '{destination_folder}' does not exist.")
        return

    print(f"Source folder: {source_folder}")
    print(f"Destination folder: {destination_folder}")
    print("-" * 60)

    try:
        source_files = [f for f in source_folder.iterdir() if f.is_file()]
    except PermissionError:
        print(f"Error: Permission denied when accessing '{source_folder}'.")
        return

    if not source_files:
        print("No files found in the source folder.")
        return

    copied_count = 0
    skipped_count = 0
    no_code_count = 0
    unknown_count = 0
    excluded_count = 0
    new_files = []
    buzon_folder = destination_folder / _BUZON_FOLDER_NAME

    for source_file in source_files:
        filename = source_file.name

        if is_excluded_from_sync(filename):
            print(f"🚫 Not synced (NOTIFICACIÓN AUTOMÁTICA): {filename}")
            excluded_count += 1
            continue

        case_code = get_case_code(filename)
        if case_code is None:
            print(f"⚠️  Skipped (no case code in filename): {filename}")
            no_code_count += 1
            continue

        case_folder = find_case_folder(destination_folder, case_code)
        if case_folder is None:
            # No matching folder — fall back to Coquibot/UNKNOWN/
            case_folder = destination_folder / "UNKNOWN"
            unknown_count += 1
            print(f"❓ No folder found for {case_code} — routing to UNKNOWN/: {filename}")

        # Target is <matched_case_folder>/SUMAC/
        sumac_folder = case_folder / "SUMAC"
        if not sumac_folder.exists():
            try:
                sumac_folder.mkdir(parents=True, exist_ok=True)
                print(f"📁 Created SUMAC folder: {sumac_folder}")
            except (PermissionError, OSError) as e:
                print(f"❌ Error creating SUMAC folder in {case_folder}: {e}")
                continue

        destination_file = sumac_folder / filename

        if destination_file.exists():
            print(f"⏭️  Skipped (already exists): {filename}")
            skipped_count += 1
        else:
            try:
                # copy2 preserves original timestamps and metadata, unlike copy
                shutil.copy2(source_file, destination_file)
                print(f"✅ Copied: {filename}")
                print(f"       → {case_folder.name}/SUMAC/")
                copied_count += 1
                new_files.append(filename)
            except (shutil.Error, PermissionError, OSError) as e:
                print(f"❌ Error copying {filename}: {e}")
                continue

            # Also drop a copy in the Mindie mailbox folder
            try:
                buzon_folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, buzon_folder / filename)
                print(f"       → {_BUZON_FOLDER_NAME}/")
            except (shutil.Error, PermissionError, OSError) as e:
                print(f"❌ Error copying {filename} to {_BUZON_FOLDER_NAME}: {e}")

    print("\n" + "=" * 60)
    print("SUMMARY:")
    print(f"  Files copied:                      {copied_count}")
    print(f"  Files skipped (already exist):     {skipped_count}")
    print(f"  Files skipped (no case code):      {no_code_count}")
    print(f"  Files not synced (auto notices):   {excluded_count}")
    print(f"  Files routed to UNKNOWN/:           {unknown_count}")
    print(f"  Total files processed:             {copied_count + skipped_count + no_code_count + unknown_count + excluded_count}")
    print("=" * 60)

    if new_files:
        _write_dropbox_log(new_files)
        _send_email(new_files)
        _send_case_emails(new_files)
    # Runs even with no new files so backlog lines queued on earlier runs go out when due.
    _process_client_emails(new_files)


def move_files_from_UNKNOWN_to_dropbox_subfolders(destination_folder=None):
    """
    Move files out of Coquibot/UNKNOWN/SUMAC/ into the correct Dropbox case
    folder, now that a matching folder may have been created since a file was
    first routed there as unmatched.

    Each file is moved to <case_folder>/SUMAC/ where <case_folder> is the
    Dropbox subfolder whose name contains the file's SUMAC case code,
    overwriting any file already there. Files with no matching case folder
    are left in UNKNOWN/SUMAC/ untouched.
    """
    if destination_folder is None:
        destination_folder = _read_dropbox_dest()
    destination_folder = Path(destination_folder)

    unknown_folder = destination_folder / "UNKNOWN" / "SUMAC"
    if not unknown_folder.exists():
        print(f"No UNKNOWN/SUMAC folder found at '{unknown_folder}' — nothing to move.")
        return

    try:
        unknown_files = [f for f in unknown_folder.iterdir() if f.is_file()]
    except PermissionError:
        print(f"Error: Permission denied when accessing '{unknown_folder}'.")
        return

    if not unknown_files:
        print("No files found in UNKNOWN/SUMAC — nothing to move.")
        return

    print(f"Scanning UNKNOWN/SUMAC ({len(unknown_files)} file(s)) for matching case folders...")
    print("-" * 60)

    moved_count = 0
    left_count = 0
    no_code_count = 0

    for source_file in unknown_files:
        filename = source_file.name

        case_code = get_case_code(filename)
        if case_code is None:
            print(f"⚠️  Left in UNKNOWN (no case code in filename): {filename}")
            no_code_count += 1
            continue

        case_folder = find_case_folder(destination_folder, case_code)
        if case_folder is None or case_folder.name == "UNKNOWN":
            print(f"❓ Still no folder found for {case_code} — leaving in UNKNOWN/: {filename}")
            left_count += 1
            continue

        sumac_folder = case_folder / "SUMAC"
        try:
            sumac_folder.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            print(f"❌ Error creating SUMAC folder in {case_folder}: {e}")
            left_count += 1
            continue

        destination_file = sumac_folder / filename
        try:
            if destination_file.exists():
                destination_file.unlink()  # overwrite
            shutil.move(str(source_file), str(destination_file))
            print(f"➡️  Moved: {filename}")
            print(f"       → {case_folder.name}/SUMAC/")
            moved_count += 1
        except (shutil.Error, PermissionError, OSError) as e:
            print(f"❌ Error moving {filename}: {e}")
            left_count += 1

    print("\n" + "=" * 60)
    print("SUMMARY:")
    print(f"  Files moved:                       {moved_count}")
    print(f"  Files left in UNKNOWN (no match):  {left_count}")
    print(f"  Files left (no case code):         {no_code_count}")
    print(f"  Total files processed:             {moved_count + left_count + no_code_count}")
    print("=" * 60)


def preview_organization(source_folder=None, destination_folder=None):
    """
    Preview where files would be copied without actually copying them.
    Useful for verifying folder matches before running for real.
    """

    if source_folder is None:
        source_folder = _SCRIPT_DIR / "sumac_documents"

    if destination_folder is None:
        destination_folder = _read_dropbox_dest()

    source_folder = Path(source_folder)
    destination_folder = Path(destination_folder)

    if not source_folder.exists():
        print(f"Error: Source folder '{source_folder}' does not exist.")
        return

    print(f"Preview — source: {source_folder}")
    print(f"Preview — destination root: {destination_folder}")
    print("-" * 60)

    source_files = [f for f in source_folder.iterdir() if f.is_file()]

    if not source_files:
        print("No files found.")
        return

    for source_file in sorted(source_files):
        filename = source_file.name
        case_code = get_case_code(filename)

        if is_excluded_from_sync(filename):
            print(f"🚫 {filename}")
            print(f"       NOTIFICACIÓN AUTOMÁTICA — would not be synced")
            continue

        if case_code is None:
            print(f"⚠️  {filename}")
            print(f"       No case code found — would be skipped")
            continue

        if destination_folder.exists():
            case_folder = find_case_folder(destination_folder, case_code)
        else:
            case_folder = None

        if case_folder is None:
            case_folder = destination_folder / "UNKNOWN"
            print(f"❓ {filename}")
            print(f"       Case code {case_code} — no match, would go to UNKNOWN/SUMAC/")
        else:
            sumac_folder = case_folder / "SUMAC"
            dest_file = sumac_folder / filename
            status = "exists, would skip" if dest_file.exists() else "would copy"
            print(f"📄 {filename}")
            print(f"       → {case_folder.name}/SUMAC/  ({status})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Copy SUMAC court files to matching Dropbox case folders",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dropbox_sync2.py                                           # default paths
  python dropbox_sync2.py --source "C:\\docs" --dest "D:\\Dropbox"  # custom paths
  python dropbox_sync2.py --preview                                 # dry-run preview
  python dropbox_sync2.py --source "C:\\docs" --preview            # preview + custom source
        """
    )

    parser.add_argument('--source', help='Source folder path (default: ./sumac_documents)')
    parser.add_argument('--dest', help='Destination Dropbox root (default: C:\\Users\\luisd\\Dropbox\\Coquibot)')
    parser.add_argument('--preview', action='store_true',
                        help='Preview routing without copying files')

    args = parser.parse_args()

    if args.preview:
        preview_organization(source_folder=args.source, destination_folder=args.dest)
    else:
        copy_files_to_dropbox_subfolders(
            source_folder=args.source,
            destination_folder=args.dest
        )
