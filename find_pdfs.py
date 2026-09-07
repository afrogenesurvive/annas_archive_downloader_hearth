#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_pdfs.py
============
Legal, open-access bulk "PDF finder" for scholarly references.

This script reads a references file (e.g. `references.txt`), parses each
scholarly reference into a (title, author, year) query, looks the work up in
OpenAlex, and - when an open-access PDF of the most recently published
edition is available - downloads it with progress reporting.

Everything is sourced from legal/open-access providers (OpenAlex open-access
locations). Paywalled items with no OA copy are logged and skipped, and the
script moves on to the next reference.

Usage
-----
    python find_pdfs.py [REFERENCES_FILE] [DOWNLOAD_DIR] [options]

Examples
--------
    python find_pdfs.py references.txt ./pdfs
    python find_pdfs.py references.txt ./pdfs --email you@example.com
    python find_pdfs.py --dry-run                      # preview only
    python find_pdfs.py references.txt ./pdfs --missing-out pending.txt

Options
-------
    --email EMAIL     Email passed to OpenAlex (polite pool). Default:
                      open-access@example.com
    --log FILE        Where to append the log. Default:
                      <DOWNLOAD_DIR>/find_pdfs_log.txt
    --dry-run         Resolve references and report what would be downloaded,
                      but do not actually fetch any files.
    --browser         If a plain-HTTP download fails, retry the OA PDF in a
                      real (headed) Chromium via Playwright.
    --wait SECONDS    Polite delay between references. Default: 1.0
    --missing-out FILE
                      Write every reference that could NOT be obtained in
                      open access to FILE as a clean, numbered list you can
                      resolve by hand (and later re-run find_pdfs.py on).
                      Default: <DOWNLOAD_DIR>/pending.txt. Use
                      --no-missing-out to disable.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OPENALEX_SEARCH = "https://api.openalex.org/works"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2/"
DEFAULT_EMAIL = ""  # user should pass their own via --email for best results
DEFAULT_REFS_FILE = "references.txt"
DEFAULT_DOWNLOAD_DIR = "pdf_downloads"
DEFAULT_LOG_NAME = "find_pdfs_log.txt"
PDF_MAGIC = b"%PDF-"
TIMEOUT = 60
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
PDF_ACCEPT = "application/pdf,text/html;q=0.9,*/*;q=0.8"

_UA_BASE = "find_pdfs/1.0 (open-access research helper; mailto:{email})"

# Stop words ignored when comparing query title vs. OpenAlex title.
_STOP = set(
    "a an and the of in on for to from with by as at or is are was were be "
    "its it their our your his her study studies using use used effects "
    "effect new novel analysis analyses learning behavior behaviour "
    "human experiment experimental results performance".split()
)


# ---------------------------------------------------------------------------
# Logging setup (console + file)
# ---------------------------------------------------------------------------
def setup_logging(log_file, console_level=logging.INFO):
    """Return a logger that writes to both the console and a file."""
    logger = logging.getLogger("find_pdfs")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # avoid duplicate handlers on re-runs

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", log_file, exc)

    return logger


# ---------------------------------------------------------------------------
# references.txt parsing
# ---------------------------------------------------------------------------
_LINE_OF_DASHES = re.compile(r"^\s*(?:[-=]+\s*)*$")
# A numbered line only STARTS a new reference if text follows the number
# (so wrapped tokens like a lone page number '445.' are not new entries).
_NUMBERED = re.compile(r"^\s*(\d{1,3})\.\s+(.*)$")
_HEADER = re.compile(
    r"^\s*(?:SOURCE\s*\d*\s*:|REFERENCES|END OF REFERENCES|PART\s+\d+)#?\s*",
    re.IGNORECASE,
)
_TITLE_QUOTED = re.compile('"([^"]+)"')
_TITLE_SINGLE = re.compile("'([^']+)'")
_YEAR_P = re.compile(r"\((\d{4})\)")
_SURNAME_P = re.compile(r"([A-Z][A-Za-z\u00C0-\u017F'\-]+)")


def _is_skip_line(line):
    """Header/footer/decorative lines are not part of any reference."""
    s = line.strip()
    if not s:
        return True
    if _LINE_OF_DASHES.match(s):
        return True
    if _HEADER.match(s):
        return True
    if s.upper() in ("REFERENCES",) or "END OF REFERENCES" in s.upper():
        return True
    return False


def _strip_notes(text):
    """Remove bracketed annotations, e.g. [INCOMPLETE - ...], which can
    otherwise contain text that looks like a title (e.g. 'Table 3.2')."""
    return re.sub(r"\[[^\]]*\]", " ", text)


def _extract_title(text):
    text = _strip_notes(text)
    m = _TITLE_QUOTED.search(text)
    if m:
        title = m.group(1).strip()
    else:
        m = _TITLE_SINGLE.search(text)
        title = m.group(1).strip() if m else None
    if title:
        title = title.strip().rstrip(".")
        if title:
            return title
    # Fallback: no quoted title -> use the sentence right after the year
    # paren, which in a citation is usually the real (italicised) title.
    ym = _YEAR_P.search(text)
    if ym:
        rest = text[ym.end():].lstrip(" ).")
        seg = re.split(r"\.\s", rest, maxsplit=1)[0].strip().rstrip(".")
        if seg and len(seg) > 3 and not seg.lower().startswith(
                ("http", "retrieved", "available")):
            return seg
    return None


def _extract_author(text):
    """Return (surname, author_string) or (None, None).
    Works on note-stripped text (a leading '[No author given]' is removed,
    which correctly yields no author)."""
    text = _strip_notes(text)
    m = _YEAR_P.search(text)
    seg = text[: m.start()] if m else text
    seg = seg.strip().strip(".").strip(" &,;").strip()
    if not seg:
        return None, None
    sm = _SURNAME_P.search(seg)
    surname = sm.group(1) if sm else None
    return surname, seg


def _extract_year(text):
    m = _YEAR_P.search(text)
    return int(m.group(1)) if m else None


def parse_references(path):
    """
    Parse a free-form references file into a list of reference dicts.

    A new reference begins at a line like "12.  Filatova, O. A., et al. ..."
    and continues until the next numbered line. Header/footer lines, dash
    separators and empty lines are ignored.

    Returns a list of dicts:
        {number, raw, title, surname, author, year, label}
    """
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    entries = []  # list of (number, text)
    cur_num, cur_text = None, []

    for raw in lines:
        line = raw.rstrip("\n")
        if _is_skip_line(line):
            continue
        m = _NUMBERED.match(line)
        if m:
            body = m.group(2).strip()
            if body:
                if cur_num is not None:
                    entries.append((cur_num, " ".join(cur_text)))
                cur_num = int(m.group(1))
                cur_text = [body]
            else:
                # number with nothing after it (e.g. a wrapped page number)
                if cur_num is not None:
                    cur_text.append(line.strip())
        else:
            if cur_num is not None:
                cur_text.append(line.strip())
    if cur_num is not None:
        entries.append((cur_num, " ".join(cur_text)))

    refs = []
    for number, text in entries:
        year = _extract_year(text)
        title = _extract_title(text)
        if not title:
            # cannot search without a title -> keep but flag
            refs.append({
                "number": number,
                "raw": text,
                "title": None,
                "surname": None,
                "author": None,
                "year": year,
                "label": f"#{number} (unparseable): {text[:90]}...",
            })
            continue
        surname, author = _extract_author(text)
        label = title
        if author:
            label += f" - {author}"
        if year:
            label += f" ({year})"
        refs.append({
            "number": number,
            "raw": text,
            "title": title,
            "surname": surname,
            "author": author,
            "year": year,
            "label": label,
        })
    return refs


# ---------------------------------------------------------------------------
# Small HTTP helper
# ---------------------------------------------------------------------------
def _fetch_json(url, params, email, tries=3):
    params = dict(params)
    if email:  # OpenAlex polite pool; omit entirely when no email is given
        params.setdefault("mailto", email)
    query = urllib.parse.urlencode(params)
    full = f"{url}?{query}"
    headers = {"User-Agent": _UA_BASE.format(email=email),
               "Accept": "application/json"}
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(full, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(2 * attempt)
                continue
            raise
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"OpenAlex request failed after {tries} tries: {last_err}")


# ---------------------------------------------------------------------------
# OpenAlex lookup + newest OA-PDF selection
# ---------------------------------------------------------------------------
def _norm_tokens(text):
    if not text:
        return []
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in _STOP and len(w) > 1]


def _token_overlap(a, b):
    ta, tb = set(_norm_tokens(a)), set(_norm_tokens(b))
    if not ta or not tb:
        return 0.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


def _work_year(work):
    if work.get("publication_date"):
        return int(work["publication_date"][:4])
    return work.get("publication_year")


def _authors_string(work):
    return ", ".join(
        a.get("author", {}).get("display_name", "")
        for a in work.get("authorships", [])
        if a.get("author", {}).get("display_name")
    )


def _is_oa(work):
    """OpenAlex v2 keeps the OA flag in an 'open_access' object."""
    oa = work.get("open_access") or {}
    if isinstance(oa, dict) and oa.get("is_oa") is not None:
        return bool(oa.get("is_oa"))
    return bool(work.get("is_oa"))  # fallback for older payloads


def oa_pdf_url(work):
    """Return the first usable open-access pdf_url for a work, if any."""
    for key in ("best_oa_location", "primary_location"):
        loc = work.get(key) or {}
        u = (loc.get("pdf_url") or "").strip()
        if u:
            return u
    for loc in work.get("locations", []) or []:
        u = (loc.get("pdf_url") or "").strip()
        if u:
            return u
    return None


def unpaywall_pdf_url(doi, email):
    """Best-effort direct publisher PDF from Unpaywall for a DOI.
    Returns None on any failure (missing record, network error, no OA copy)
    so that callers can fall back to the OpenAlex URL."""
    if not (doi and email):
        return None
    doi_path = urllib.parse.quote(doi, safe="")
    url = (f"{UNPAYWALL_BASE}{doi_path}?email="
           f"{urllib.parse.quote(email)}")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    loc = data.get("best_oa_location") or {}
    return loc.get("url_for_pdf") or loc.get("url") or None


def _match_score(work, ref):
    """Rough relevance score (0..1) of an OpenAlex work vs. the reference."""
    wt = work.get("title") or ""
    if not wt:
        return 0.0
    ov = _token_overlap(wt, ref["title"])
    score = ov
    # year bonus if it matches the cited year closely
    wy = _work_year(work)
    if ref["year"] and wy:
        if abs(wy - ref["year"]) <= 1:
            score += 0.15
        elif abs(wy - ref["year"]) <= 3:
            score += 0.05
    # author surname in title or author list is a strong signal
    if ref["surname"]:
        author_txt = _authors_string(work).lower()
        hay = f"{wt.lower()} {author_txt}"
        if ref["surname"].lower() in hay:
            score += 0.25
    return score


def lookup_openalex(ref, email):
    """
    Search OpenAlex for the reference and select the *newest* result that
    has an open-access PDF.

    Returns a dict:
        {found: bool, work: dict|None, pdf_url: str|None,
         reason: str, candidates_seen: int}
    """
    q = ref["title"]
    if ref["surname"]:
        q += f" {ref['surname']}"
    data = _fetch_json(
        OPENALEX_SEARCH,
        {"search": q, "per-page": 25, "select": ("id,doi,title,display_name,"
                                                  "publication_year,publication_date,"
                                                  "open_access,authorships,"
                                                  "best_oa_location,primary_location,"
                                                  "locations")},
        email,
    )
    results = data.get("results", [])
    if not results:
        return {"found": False, "work": None, "pdf_url": None,
                "reason": "No results from OpenAlex", "candidates_seen": 0}

    # Keep only reasonably relevant matches: a high title-overlap is the main
    # gate. A slightly lower overlap is only accepted when the author surname
    # literally appears in the matched title (strong signal of the same work).
    scored = []
    for w in results:
        wt = w.get("title") or ""
        ov = _token_overlap(wt, ref["title"])
        ok = ov >= 0.55
        if not ok and ref["surname"]:
            surname_tokens = _norm_tokens(ref["surname"])
            if surname_tokens and set(surname_tokens) <= set(_norm_tokens(wt)):
                ok = ov >= 0.4
        if ok:
            scored.append((_match_score(w, ref), w))

    if not scored:
        return {"found": False, "work": None, "pdf_url": None,
                "reason": f"No close title match among {len(results)} results",
                "candidates_seen": len(results)}

    # --- Selection -------------------------------------------------------
    # Prefer results whose publication year is close to the cited year
    # (those are "the same work/edition"). Only fall back to far-away years
    # when no close-year match exists AND the far match carries strong
    # title/author evidence, so we never grab an unrelated OA paper just
    # because it shares a few keywords.
    ref_year = ref["year"]

    def newest_oa(pairs):
        """Return (work, pdf_url) of the most recent OA-pdf entry, else None."""
        oa_pairs = []
        for _s, w in pairs:
            u = oa_pdf_url(w)
            if u:
                oa_pairs.append((w, u))
        if not oa_pairs:
            return None, None
        oa_pairs.sort(key=lambda t: (_work_year(t[0]) or 0,
                                     t[0].get("publication_date") or ""))
        return oa_pairs[-1]

    if ref_year:
        near = [(s, w) for (s, w) in scored
                if (wy := _work_year(w)) is not None and abs(wy - ref_year) <= 1]
        far = [(s, w) for (s, w) in scored if (s, w) not in near]
    else:
        near, far = [], scored

    if ref_year and near:
        # (1) close-year matches -> treat those as the work / its editions
        work, url = newest_oa(near)
        if work:
            return {"found": True, "work": work, "pdf_url": url,
                    "reason": f"Newest OA edition among {len(near)} close-year "
                              f"matches (year {_work_year(work)})",
                    "candidates_seen": len(results)}
        # (1b) close matches are paywalled, but an essentially identical-title
        #      OA version exists at another year (reprint/newer edition)
        same_far = [(s, w) for (s, w) in far
                    if _token_overlap((w.get("title") or ""), ref["title"]) >= 0.7]
        work, url = newest_oa(same_far)
        if work:
            return {"found": True, "work": work, "pdf_url": url,
                    "reason": "Same-title OA edition found at a different year",
                    "candidates_seen": len(results)}
        best = max(near, key=lambda t: t[0])[1]
        return {"found": False, "work": best, "pdf_url": None,
                "reason": ("Found a close match but no open-access PDF is "
                           "available (paywalled)"),
                "candidates_seen": len(results)}

    # (2) no close-year match (or no year given): accept other-year results
    #     ONLY when they are near-identical in title.
    strong_pairs = [(s, w) for (s, w) in scored
                    if _token_overlap((w.get("title") or ""), ref["title"]) >= 0.6]
    work, url = newest_oa(strong_pairs)
    if work:
        return {"found": True, "work": work, "pdf_url": url,
                "reason": (f"Newest OA match of {len(strong_pairs)} candidates "
                           "(no close-year match)"),
                "candidates_seen": len(results)}

    # (3) nothing downloadable
    if scored:
        best = max(scored, key=lambda t: t[0])[1]
        return {"found": False, "work": best, "pdf_url": None,
                "reason": ("No downloadable open-access result (close matches "
                           "paywalled, or no strong OA match elsewhere)"),
                "candidates_seen": len(results)}
    return {"found": False, "work": None, "pdf_url": None,
            "reason": f"No close title match among {len(results)} results",
            "candidates_seen": len(results)}


# ---------------------------------------------------------------------------
# Download with progress
# ---------------------------------------------------------------------------
def _safe_filename(label, year):
    base = label.split(" (", 1)[0]
    name = re.sub(r"[\\/:*?\"<>|]+", "_", base).strip().strip(".")
    name = re.sub(r"\s+", " ", name)[:120]
    if year:
        name = f"{name} ({year})"
    return f"{name}.pdf"


def download_pdf(pdf_url, dest_path, logger, dry_run=False):
    """
    Stream the PDF to dest_path, reporting progress to console and log.
    Returns (ok, message).
    """
    headers = {"User-Agent": BROWSER_UA, "Accept": PDF_ACCEPT}
    if dry_run:
        return True, "DRY-RUN: would download (not fetched)"

    try:
        req = urllib.request.Request(pdf_url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            chunk = 64 * 1024
            written = 0
            last_pct = -1
            with open(dest_path, "wb") as out:
                while True:
                    block = resp.read(chunk)
                    if not block:
                        break
                    out.write(block)
                    written += len(block)
                    if total:
                        pct = written * 100 // total
                        if pct // 5 != last_pct // 5 and pct < 100:
                            last_pct = pct
                            print(f"    Downloading ... {pct}% "
                                  f"({written / 1e6:.1f}/{total / 1e6:.1f} MB)",
                                  flush=True)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.reason} from {pdf_url}"
    except urllib.error.URLError as exc:
        return False, f"Network error: {exc.reason}"
    except OSError as exc:
        return False, f"File/socket error: {exc}"

    # Validate it is really a PDF.
    ok, msg = _validate_pdf(dest_path)
    if not ok:
        os.remove(dest_path)
        return False, msg
    return True, f"Saved {os.path.basename(dest_path)} ({written / 1e6:.2f} MB)"


def _validate_pdf(path):
    """Return (True, '') if path starts with the PDF magic bytes."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
        if not head.startswith(PDF_MAGIC):
            return False, ("Downloaded content is not a PDF (file removed); "
                           "the source likely served an HTML/JS-gated page or "
                           "an error page instead of the file")
        return True, ""
    except OSError as exc:
        return False, f"Could not validate PDF: {exc}"


def browser_download(pdf_url, dest_path, logger):
    """
    Opt-in fallback: fetch an OA PDF using a real (headed) Chromium via
    Playwright. Some publishers/repositories serve PDFs only to real
    browsers (bot protection, or a JS 'preparing download' step), which
    plain urllib cannot pass. Only ever fetches open-access content.

    Returns (ok, message).
    """
    logger.info("    Browser fallback: opening Chromium to capture PDF ...")
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except Exception as exc:
        return False, f"Playwright not available: {exc}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(user_agent=BROWSER_UA,
                                          accept_downloads=True, locale="en-US")
            page = context.new_page()
            resp = None
            try:
                resp = page.goto(pdf_url, wait_until="domcontentloaded",
                                 timeout=30000)
            except Exception as exc:
                logger.warning("    Browser navigation issue: %s", exc)

            # Case 1: the page itself is the PDF (arXiv, repositories, etc.).
            if resp is not None:
                ctype = (resp.headers.get("content-type") or "").lower()
                if ctype.startswith("application/pdf") or \
                        resp.url.lower().endswith(".pdf"):
                    body = resp.body()
                    with open(dest_path, "wb") as out:
                        out.write(body)
                    browser.close()
                    ok_v, msg_v = _validate_pdf(dest_path)
                    if ok_v:
                        return True, (f"Saved {os.path.basename(dest_path)} "
                                      f"via browser ({len(body) / 1e6:.2f} MB)")
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
                    return False, msg_v

            # Case 2: the page triggers (or shows a link/button for) a
            # download event.
            try:
                with page.expect_download(timeout=20000) as dl_info:
                    try:
                        btn = page.locator(
                            'a:has-text("Download PDF"), '
                            'a[href$=".pdf"], a[href*="download"], '
                            'button:has-text("Download")').first
                        btn.click(force=True)
                    except Exception:
                        pass  # rely on the automatic download
                dl = dl_info.value
                dl.save_as(dest_path)
                browser.close()
            except PwTimeout:
                browser.close()
                return False, ("Browser fallback timed out waiting for the "
                               "download to start")
            except Exception as exc:
                browser.close()
                return False, f"Browser fallback failed: {exc}"

    except Exception as exc:
        return False, f"Could not launch browser: {exc}"

    ok, msg = _validate_pdf(dest_path)
    if not ok:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return False, msg
    size = os.path.getsize(dest_path) / 1e6
    return True, (f"Saved {os.path.basename(dest_path)} via browser "
                  f"({size:.2f} MB)")


# ---------------------------------------------------------------------------
# Missing-reference export (pending.txt)
# ---------------------------------------------------------------------------
def _missing_citation(ref):
    """One clean, re-parseable citation line for a reference that was not
    obtained in open access, so the written list can be handed back to this
    script (or read by a person) once the gaps are fixed. Prefers parsed
    fields and falls back to the note-stripped raw entry text."""
    title = (ref.get("title") or "").strip()
    author = (ref.get("author") or "").strip()
    year = ref.get("year")
    if title and (author or year):
        if author:
            name = author.rstrip().rstrip(".")
            head = "%s. (%d)." % (name, year) if year else "%s." % name
        elif year:
            head = "(%d)." % year
        else:
            head = ""
        return '%s "%s".' % (head, title)
    raw = re.sub(r"\s+", " ", _strip_notes(ref.get("raw") or "")).strip()
    if raw:
        return raw
    return (ref.get("label") or "").strip()


def write_pending(missing, path, logger):
    """Write the references that could NOT be obtained in open access to a
    clean, numbered list. The header/footer/dash lines are skipped by
    parse_references(), so the file stays re-parseable by this script.
    Returns the number of lines written (0 when nothing is missing)."""
    if not missing:
        logger.info("No references missing; not writing %s", path)
        return 0
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("References without an open-access PDF copy "
                     "(resolve manually, then re-run find_pdfs.py)\n")
            fh.write("-" * 40 + "\n\n")
            for i, ref in enumerate(missing, 1):
                fh.write("%d.  %s\n" % (i, _missing_citation(ref)))
            fh.write("\n" + "-" * 40 + "\n")
        logger.info("Wrote %d missing reference(s) to %s", len(missing), path)
    except OSError as exc:
        logger.error("Could not write missing list %s: %s", path, exc)
        return 0
    return len(missing)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run(args):
    # ensure the download dir exists before logging opens a file inside it
    if not os.path.exists(args.download_dir):
        os.makedirs(args.download_dir, exist_ok=True)
    logger = setup_logging(args.log_file)
    if args.email:
        logger.info("Using polite-pool email: %s", args.email)
    else:
        logger.warning("No --email given: Unpaywall direct-PDF resolution is "
                       "disabled (OpenAlex is still used). Pass "
                       "--email your@address for best results.")

    if not os.path.exists(args.refs_file):
        logger.error("References file not found: %s", args.refs_file)
        return 1

    refs = parse_references(args.refs_file)
    if not refs:
        logger.error("No references could be parsed from %s", args.refs_file)
        return 1

    logger.info("Loaded %d references from %s", len(refs), args.refs_file)
    if args.dry_run:
        logger.info("DRY-RUN mode: nothing will be downloaded.")

    n_downloaded = n_no_oa = n_not_found = n_failed = n_skipped = 0
    missing = []

    for i, ref in enumerate(refs, 1):
        # (a) which publication is being considered
        logger.info("=" * 70)
        logger.info("[%d/%d] Considering: %s", i, len(refs), ref["label"])

        if ref["title"] is None:
            logger.warning("  -> Could not parse a title from this entry; skipping.")
            n_not_found += 1
            missing.append(ref)
            continue

        # (b) stage: searching
        logger.info("  [stage] searching OpenAlex for: %s %s",
                    ref["title"], ref["surname"] or "")
        try:
            res = lookup_openalex(ref, args.email)
        except Exception as exc:  # API outage should not kill the run
            logger.error("  [stage] search error: %s", exc)
            n_failed += 1
            missing.append(ref)
            continue

        if not res["found"]:
            # (c) result: not found
            logger.warning("  [result] NOT FOUND - %s", res["reason"])
            n_no_oa += 1
            missing.append(ref)
            time.sleep(args.wait)
            continue

        work, pdf_url = res["work"], res["pdf_url"]
        logger.info("  [result] FOUND: %s", work.get("title"))
        logger.info("           author(s): %s", _authors_string(work))
        logger.info("           year: %s  doi: %s",
                    _work_year(work) or "?", work.get("doi"))
        if _is_oa(work):
            logger.info("           open access: yes (%s)",
                        (work.get("best_oa_location") or {}).get("license")
                        or (work.get("open_access") or {}).get("oa_status")
                        or "unspecified")
        logger.info("           pdf: %s", pdf_url)
        # Prefer a direct publisher PDF from Unpaywall when it can provide one
        # (some OpenAlex URLs are JS-gated landing pages, not raw PDFs).
        uw = unpaywall_pdf_url(work.get("doi"), args.email)
        if uw:
            logger.info("           unpaywall direct pdf: %s", uw)
            pdf_url = uw

        # pick filename
        year = _work_year(work) or ref["year"]
        fname = _safe_filename(work.get("title") or ref["title"], year)
        dest = os.path.join(args.download_dir, fname)

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            logger.info("  [skip] already present: %s", dest)
            n_skipped += 1
            time.sleep(args.wait)
            continue

        # (b) stage: downloading / waiting
        logger.info("  [stage] downloading -> %s", dest)
        ok, msg = download_pdf(pdf_url, dest, logger, dry_run=args.dry_run)
        if not ok and args.use_browser and not args.dry_run:
            logger.warning("  [stage] plain HTTP failed (%s); retrying the OA "
                           "PDF in a real browser (--browser)", msg)
            ok, msg = browser_download(pdf_url, dest, logger)
        if ok:
            # (e) success
            logger.info("  [done] DOWNLOAD OK - %s", msg)
            n_downloaded += 1
        else:
            # (e) failure + reason
            logger.error("  [done] DOWNLOAD FAILED - %s", msg)
            n_failed += 1
            missing.append(ref)

        time.sleep(args.wait)

    # Summary
    logger.info("=" * 70)
    logger.info("Finished processing %d references.", len(refs))
    logger.info("  Downloaded / would download : %d", n_downloaded)
    logger.info("  Skipped (already present)   : %d", n_skipped)
    logger.info("  No open-access PDF          : %d", n_no_oa)
    logger.info("  Failed / errors             : %d", n_failed)
    logger.info("  Missing (OA gap)            : %d", len(missing))

    if args.write_missing:
        missing_path = args.missing_out or os.path.join(
            args.download_dir, "pending.txt")
        write_pending(missing, missing_path, logger)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        description="Find & download the newest open-access PDF edition of "
                    "each reference in a references file (via OpenAlex).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----\n", 1)[-1],
    )
    p.add_argument("refs_file", nargs="?", default=DEFAULT_REFS_FILE,
                   help=f"references file to parse (default: {DEFAULT_REFS_FILE})")
    p.add_argument("download_dir", nargs="?",
                   default=DEFAULT_DOWNLOAD_DIR,
                   help=f"folder to save PDFs into (default: {DEFAULT_DOWNLOAD_DIR})")
    p.add_argument("--email", default=DEFAULT_EMAIL,
                   help="your email address for OpenAlex/Unpaywall polite "
                        "pools (recommended; required for Unpaywall direct-"
                        "PDF resolution)")
    p.add_argument("--log", dest="log_file", default=None,
                   help=f"log file path (default: <download_dir>/{DEFAULT_LOG_NAME})")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve only; do not download anything")
    p.add_argument("--browser", dest="use_browser", action="store_true",
                   help="retry failed OA-PDF downloads in a real (headed) "
                        "Chromium via Playwright")
    p.add_argument("--wait", type=float, default=1.0,
                   help="politeness delay between references, seconds")
    p.add_argument("--missing-out", dest="missing_out", default=None,
                   metavar="FILE",
                   help="write references with no open-access PDF to FILE "
                        "(default: <download_dir>/pending.txt)")
    p.add_argument("--no-missing-out", dest="write_missing",
                   action="store_false", default=True,
                   help="do not write the pending.txt missing-reference list")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.log_file:
        args.log_file = os.path.join(args.download_dir, DEFAULT_LOG_NAME)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
