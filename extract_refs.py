#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_refs.py
===============
Extract articles, scholarly references and other published references from a
plain-text (.txt) or Markdown (.md) document.

Given a research document, this tool produces a readable bibliography of every
published source it can find: journal articles, preprints, books and chapters,
theses, conference papers, reports, and web sources. It understands several of
the ways references appear in a document:

  * formal bibliographic entries (numbered lists, APA/MLA/Chicago-style lines),
  * inline parenthetical citations such as "(Smith & Jones, 2020)" and
    narrative ones like "Smith et al. (2020) ...",
  * bracketed / Vancouver-style citations ("[1]", "[2,3]") and their lists,
  * hyperlinks and identifiers (Markdown links, bare URLs, DOI / arXiv /
    PubMed IDs).

Every detected reference is merged with duplicate mentions, de-duplicated,
classified by type, and annotated with where it was cited. Anything that is
incomplete or could not be confidently resolved is never guessed: it is placed
in a clearly-marked "needs review" section for you to fix by hand.

The tool only reads the file you give it; it makes no network requests and
needs no third-party packages (Python standard library only).

Usage
-----
    python extract_refs.py paper.md                        # print Markdown to stdout
    python extract_refs.py notes.txt -o bibliography.md    # write a report file
    python extract_refs.py paper.md --format txt           # plain-text report
    python extract_refs.py paper.md --order alpha          # alphabetise by author
    python extract_refs.py paper.md --no-context           # drop section annotations
    python extract_refs.py paper.md --no-dedupe            # keep every raw hit
"""

import argparse
import os
import re
import sys
import bisect
import logging
from datetime import date

# ---------------------------------------------------------------------------
# Small constants / regular expressions
# ---------------------------------------------------------------------------

# "Reference list" section headings that switch the scanner into bibliography
# mode (full entries) rather than prose mode (inline citations).
_REF_HEADINGS = {
    "references", "bibliography", "works cited", "literature cited",
    "references cited", "reference list", "cited works", "sources",
    "further reading",
}
# Standalone lines (not Markdown headings) that also switch to bibliography mode.
_PLAIN_REF_LINE = re.compile(
    r"^\s*(?:REFERENCES|BIBLIOGRAPHY|WORKS\s+CITED|LITERATURE\s+CITED|"
    r"REFERENCES\s+CITED|END\s+OF\s+REFERENCES|SOURCE[S]?)\s*:?\s*$",
    re.IGNORECASE,
)

_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_RULE = re.compile(r"^\s*(?:[-=*_]\s*){2,}$")
_IMAGE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_HTML_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")

# Entry starters inside a bibliography segment.
_NUMBERED = re.compile(r"^\s*(?:(\d{1,4})[.)]|\[(\d{1,4})\]|\((\d{1,4})\))\s+(.*)$")
# APA-style author prefix ending in (year) + terminal punctuation.
# Components: Surname (+ optional second capital word for institutional authors)
# + optional initials (", J.", ", W. F.") + optional ", et al.".  Between
# components the separator may be ", & ", " & ", " and ", or ", ".
_APA_START = re.compile(
    r"^(?P<prefix>"
    r"(?:[A-Z][A-Za-z\u00C0-\u017F'\-]+(?:[\s]+[A-Z][A-Za-z\u00C0-\u017F'\-]+)?"
    r"(?:[\s]*,[\s]*[A-Z]\.(?:[\s]*[A-Z]\.?)?)?"
    r"(?:[\s,]*et[\s]+al\.?)?)"
    r"(?:"
    r"(?:[\s]*,[\s]*(?:&|and)[\s]+"     # ", & " / ", and "
    r"|[\s]+(?:&|and)[\s]+"
    r"|[\s]*,[\s]*)"
    r"[A-Z][A-Za-z\u00C0-\u017F'\-]+"
    r"(?:[\s]*,[\s]*[A-Z]\.(?:[\s]*[A-Z]\.?)?)?"
    r"(?:[\s,]*et[\s]+al\.?)?"
    r")*"
    r")\s*\((?P<year>\d{4})\)\s*(?P<end>[.,;:])"
)
_NO_AUTHOR = re.compile(r"^\s*\[(?:no\s+author|no\s+author\s+given|n\.?\s*a\.?)\]",
                        re.IGNORECASE)

# Year / quoted-title helpers.
_YEAR_PAREN = re.compile(r"\((\d{4})\)")
_YEAR_BARE = re.compile(r"(?<!\d)(?:19|20)\d{2}[a-z]?(?!\d)")
_QUOTED_TITLE = re.compile(r'"([^"]{3,})"')
_SQUOTED_TITLE = re.compile(r"'([^']{3,})'")
_VOL_ISS = re.compile(
    r",\s*(\d{1,4})\s*\(\s*(\d{1,4})\s*\)\s*[,:]\s*"
    r"(?:pp?\.?\s*)?([\d\u2013\-eexivv]+)")   # , 35(2), 217-234
_VOL_PAGES = re.compile(
    r",\s*(\d{1,4})\s*[,:]\s*(?:pp?\.?\s*)?([\d\u2013\-eexivv]+)")  # , 175, 104106

# Inline citation shapes (in text, outside the bibliography).
_PAREN_GROUP = re.compile(r"\(([^()]{1,160})\)")
_NARRATIVE = re.compile(
    r"(?<![@\w])("
    r"[A-Z][A-Za-z\u00C0-\u017F'\-]+"                # first surname
    r"(?:[\s]+et[\s]+al\.?)?"
    r"(?:[\s]+(?:&|and)[\s]+[A-Z][A-Za-z\u00C0-\u017F'\-]+)?"
    r")\s*\((\d{4}[a-z]?)\)")
_INLINE_PREFIXES = (
    "e.g.,", "e.g.", "eg,", "i.e.,", "i.e.", "see also", "see", "cf.", "cf",
    "but see", "compare", "for a review", "for a recent review",
    "for a discussion", "for a recent discussion", "for example",
    "for details", "for details see", "for an overview", "for a summary",
    "e.g. in", "particularly", "especially",
)

# URLs / persistent identifiers.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s)\]}<>\"']+")
_DOI_RAW = re.compile(r"(?<![@\w])10\.\d{4,9}/[^\s)\]}<>\"']+")
_ARXIV = re.compile(
    r"\b(?:arXiv|arxiv)\s*:\s*([a-z\-]+(?:\.[A-Z]{1,2})?/\d{4}\.\d+(?:v\d+)?|"
    r"\d{4}\.\d+(?:v\d+)?)")
_PMID = re.compile(r"\b(?:PMID|PubMed\s+ID)\s*:?\s*(\d{5,9})\b", re.IGNORECASE)
_DOI_URL = re.compile(r"(?:doi\.org/|doi:|dx\.doi\.org/)")

# Bracketed editorial notes, e.g. [INCOMPLETE - ...] / [No author given].
_NOTE = re.compile(r"\[([^\]]*)\]")

# Kinds and their display order in the report.
_KIND_LABEL = {
    "journal": "Journal articles",
    "preprint": "Preprints",
    "book": "Books & chapters",
    "thesis": "Theses & dissertations",
    "conference": "Conference papers",
    "report": "Reports & working papers",
    "web": "Web sources",
    "other": "Other / unclassified",
}
_KIND_ORDER = ["journal", "preprint", "book", "thesis", "conference",
               "report", "web", "other"]


# ---------------------------------------------------------------------------
# Logging (progress + summary go to stderr so the report on stdout stays clean)
# ---------------------------------------------------------------------------
def setup_logging(level=logging.INFO):
    logger = logging.getLogger("extract_refs")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(levelname)-7s | %(message)s"))
    logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Document segmentation
# ---------------------------------------------------------------------------
def read_lines(path):
    """Return [(line_number, text_without_newline), ...] for a UTF-8 file."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return [(i + 1, line.rstrip("\n").rstrip("\r"))
                for i, line in enumerate(fh)]


def _heading_title(text):
    """Normalise a heading line to a short section label."""
    m = _MD_HEADING.match(text)
    if m:
        return m.group(2).strip()
    return text.strip().strip(":").strip()


def segment_document(lines):
    """
    Split the document into segments of two kinds:
      * kind="prose"     - normal body text (scan for inline citations/links)
      * kind="references" - a reference-list region (parse full entries)
    Each segment carries the Markdown/plain heading it lives under.
    """
    segments = []
    current = None
    code_fence = None
    heading = None
    kind = "prose"

    def flush():
        nonlocal current
        if current and current["lines"]:
            segments.append(current)
        current = None

    for num, text in lines:
        stripped = text.strip()

        # fenced code blocks
        if code_fence is not None:
            if _FENCE.match(text) and stripped.startswith(code_fence):
                code_fence = None
            continue
        fence = _FENCE.match(text)
        if fence:
            code_fence = fence.group(1)[0] * 3
            continue

        if not stripped or _RULE.match(text) or _HTML_COMMENT.match(text) \
                or _IMAGE.match(text):
            flush()
            continue

        m = _MD_HEADING.match(text)
        if m:
            flush()
            heading = _heading_title(text)
            kind = "references" if heading.lower() in _REF_HEADINGS else "prose"
            current = {"kind": kind, "heading": heading, "lines": []}
            continue
        if _PLAIN_REF_LINE.match(text):
            flush()
            heading = stripped.rstrip(":").strip()
            kind = "references"
            current = {"kind": kind, "heading": heading, "lines": []}
            continue

        if current is None:
            current = {"kind": kind, "heading": heading, "lines": []}
        current["lines"].append((num, text))

    flush()
    # If the file never had a "References" heading, treat any trailing run of
    # strong bibliographic lines as a references region anyway (handled later
    # in detection), so nothing here is lost.
    return segments


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------
def strip_notes(text):
    """Remove [ ... ] editorial brackets but keep their content separately."""
    return _NOTE.sub(" ", text)


_NOTE_SKIP = re.compile(r"^\s*(?:\d+|[nN]o\s+author(?:\s+given)?|n\.?\s*a\.?|s\.?\s*n\.?)\s*$")


def note_contents(text):
    """Return bracketed editorial notes, ignoring entry numbers like '[1]'
    and '[No author given]' markers (those are parsed separately)."""
    return [n for n in _NOTE.findall(text) if n.strip() and not _NOTE_SKIP.match(n)]


def first_surname(author_string):
    """Best-effort first author's surname. Handles 'Chen, X., & Reed, P.',
    'Winding C, et al', 'Mazur & Odum', 'Nature Portfolio', ..."""
    if not author_string:
        return None
    head = author_string.split(",", 1)[0]
    head = head.split("&", 1)[0].split("and", 1)[0]
    words = re.findall(r"[A-Za-z\u00C0-\u017F'\-]+", head)
    for w in words:
        if len(w) >= 2 and w[0].isupper():
            return w
    return words[0] if words else None


def norm_title(text):
    if not text:
        return ""
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "and", "the", "of", "in", "on", "for", "to", "with",
            "from", "by", "at", "its", "or", "as"}
    return " ".join(w for w in words if w not in stop and len(w) > 1)


def surname_token(text):
    """If text is a single clean surname, return it lower-cased, else None."""
    text = text.strip().strip(".,;:")
    if re.fullmatch(r"[A-Z][A-Za-z\u00C0-\u017F'\-]+", text):
        return text.lower()
    return None


def _line_at(offmap, offset):
    """Map a character offset in flowed text back to a source line number."""
    if not offmap:
        return None
    i = bisect.bisect_right(offmap, (offset,)) - 1
    return offmap[max(i, 0)][1]


# ---------------------------------------------------------------------------
# Inline citations (parenthetical + narrative)
# ---------------------------------------------------------------------------
def _strip_inline_prefix(clause):
    changed = True
    s = clause.strip()
    while changed:
        changed = False
        low = s.lower()
        for p in _INLINE_PREFIXES:
            if low.startswith(p):
                s = s[len(p):].lstrip(" ,:;-")
                changed = True
                break
    return s.strip(" ,")


def _clean_authors(txt):
    """
    Turn an in-text author fragment ("Mazur & Odum", "Chen et al.",
    "Filatova et al.", "Smith and Jones") into a clean surname string.
    Returns None when the fragment is not a plausible author list (so titles,
    e.g. "(A textbook on learning, 2020)", are not mistaken for citations).
    """
    txt = txt.strip().strip("()").strip(" ,")
    if not txt:
        return None
    txt = re.sub(r"^(?:editors?|eds\.?)\s*", "", txt, flags=re.IGNORECASE)
    bits = re.split(r"\s*(?:&|and|,)\s*|\s+et\s+al\.?", txt)
    names = []
    for bit in bits:
        bit = bit.strip(" .,;:()")
        if not bit:
            continue
        tok = surname_token(bit)
        if tok:
            names.append(bit)
        elif re.fullmatch(r"[A-Z]\.?", bit):  # initials, e.g. "J." "A.A"
            continue
        else:
            return None
    if not names or len(names) > 4:
        return None
    return ", ".join(names)


def parse_parenthetical(core):
    """Parse the inside of "( ... )" into (authors, years) pairs."""
    results = []
    for clause in re.split(r"\s*;\s*", core):
        clause = _strip_inline_prefix(clause)
        first = _YEAR_BARE.search(clause)
        if not first:
            continue
        years = [m.group(0) for m in _YEAR_BARE.finditer(clause)]
        authors = _clean_authors(clause[:first.start()])
        if not authors:
            continue
        int_years = sorted({int(y[:4]) for y in years})
        results.append((authors, int_years))
    return results


def parse_narrative(match):
    """Turn a narrative "Smith et al. (2020)" regex match into data."""
    authors = _clean_authors(match.group(1))
    if not authors:
        return None
    year = match.group(2)
    return authors, [int(year[:4])]


# ---------------------------------------------------------------------------
# Full bibliographic entry parsing
# ---------------------------------------------------------------------------
def _split_quoted(after):
    """If a quoted title leads the text after the year, return (title, rest)."""
    m = _QUOTED_TITLE.search(after) or _SQUOTED_TITLE.search(after)
    if m and m.start() == 0:
        return m.group(1).strip().rstrip("."), after[m.end():].strip()
    return None, after


def _journal_bounds(text):
    """Locate a volume clause within `text`. `text` may start with the journal
    name (after a quoted title) or with the title itself (unquoted style).
    Returns (prefix_text, volume, issue, pages) or None when no clause is
    present."""
    m = _VOL_ISS.search(text)
    if m:
        return text[:m.start()].strip(" ,."), m.group(1), m.group(2), m.group(3)
    m = _VOL_PAGES.search(text)
    if m:
        return text[:m.start()].strip(" ,."), m.group(1), None, m.group(2)
    return None


def _classify(rec, text):
    """Pick a kind for a record based on structural cues. Classification uses
    the parsed title/source (not the raw line), so parenthetical editorial
    annotations cannot accidentally trigger keywords."""
    low = " ".join(x for x in (rec.get("title"), rec.get("source"))
                    if x).lower()
    url = (rec.get("url") or "").lower()
    title = (rec.get("title") or "").lower()

    # identifiers / hosts first
    if rec.get("arxiv") or "arxiv.org" in url or "biorxiv" in url \
            or "medrxiv" in url or "ssrn.com" in url:
        return "preprint"
    if "pmid" in url or "pubmed" in url or "ncbi.nlm.nih.gov" in url:
        return "journal"
    if re.search(r"\b(thesis|dissertation)\b", low):
        return "thesis"
    if re.search(r"\b(proceedings|conference paper|conference proceedings"
                 r"|intl?\.?\s+conf)", low):
        return "conference"
    if re.search(r"\b(report|working paper|technical report|staff paper"
                 r"|white paper|w\.?p\.?)\b", low) \
            and not rec.get("volume"):
        return "report"
    if rec.get("volume") or "retrieved" not in low:
        # A parsed journal clause / volume is a strong journal signal.
        if rec.get("volume"):
            return "journal"
    if re.search(r"\b(in|chapter|pp?\.)\b", low) or re.search(
            r"\b(university press|publisher|edition|ed\.)\b", low):
        return "book"
    if url and not rec.get("volume"):
        # generic web page / news / link-only source (keep a real title's org)
        host = re.sub(r"^www\.", "", (urlparse_host(url) or ""))
        if host and title and not rec.get("source"):
            rec["source"] = host
        return "web"
    if title:
        return "book"
    return "other"


def urlparse_host(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return None


def _digest(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def parse_entry(text):
    """
    Parse one full bibliographic entry (possibly multi-line, already joined)
    into a record dict. Best-effort: whatever cannot be extracted is left None
    and collected as an 'issue' by the caller.
    """
    notes = note_contents(text)
    note = "; ".join(x for x in notes if x.strip())[:300] or None
    cleaned = strip_notes(text)
    rec = {"authors": None, "year": None, "title": None, "source": None,
           "volume": None, "issue": None, "pages": None, "doi": None,
           "url": None, "arxiv": None, "pmid": None, "kind": "other",
           "note": note, "raw": text}
    if not cleaned.strip():
        return rec

    # pull out identifiers / links first (they don't depend on citation style)
    for u in _BARE_URL.findall(cleaned):
        if not rec["url"]:
            rec["url"] = u.rstrip(".,;:)")
    for d in _DOI_RAW.findall(cleaned):
        if not rec["doi"]:
            rec["doi"] = d.rstrip(".,;:)")
    arx = _ARXIV.search(cleaned)
    if arx and not rec["arxiv"]:
        rec["arxiv"] = arx.group(1)
    pm = _PMID.search(cleaned)
    if pm and not rec["pmid"]:
        rec["pmid"] = pm.group(1)

    work = cleaned
    # drop trailing URL / doi text we already captured
    if rec["url"]:
        work = work.replace(rec["url"], " ").replace("(http", "(")

    # strip leading numbering ("10.", "[12]") if the caller kept it
    m_num = _NUMBERED.match(work)
    if m_num and m_num.group(4):
        work = m_num.group(4)
    # no-author marker (must be checked after the number is removed)
    no_author = False
    if _NO_AUTHOR.match(work):
        no_author = True
        work = _NO_AUTHOR.sub(" ", work)

    # Vancouver style: "Authors. Title. Journal. Year;vol(iss):pages."
    if ";" in work and _YEAR_BARE.search(work) and not _YEAR_PAREN.search(work) \
            and "." in work:
        segs = [s.strip() for s in re.split(r"\.\s+", work) if s.strip()]
        if len(segs) >= 3 and rec["authors"] is None:
            rec["authors"] = segs[0].strip(".")
            rec["title"] = segs[1].strip(".")
            if len(segs) >= 3:
                rest = " ".join(segs[2:])
                ym = re.search(r"(\d{4})\s*;\s*(\d+)(?:\((\d+)\))?\s*:\s*([\d\-e]+)", rest)
                if ym:
                    rec["year"] = int(ym.group(1))
                    rec["volume"] = ym.group(2)
                    rec["issue"] = ym.group(3)
                    rec["pages"] = ym.group(4)
                    rec["source"] = rest[:ym.start()].strip(" ;,.")
                else:
                    y = _YEAR_BARE.search(rest)
                    if y:
                        rec["year"] = int(y.group(0)[:4])
                    rec["source"] = rest[:20]
            rec["kind"] = _classify(rec, text)
            return rec

    # APA style: author prefix ... (year). title...
    m = _APA_START.match(work)
    if m and rec["authors"] is None:
        rec["authors"] = re.sub(r"\s+", " ", m.group("prefix").strip())
        rec["year"] = int(m.group("year"))
        after = work[m.end():].strip()
        title, rest = _split_quoted(after)
        if title is not None:
            rec["title"] = title.strip()
            rest_low = rest.lower()
            if rest:
                jb = _journal_bounds(rest)
                if jb:
                    journal, vol, issue, pages = jb
                    rec["source"], rec["volume"] = journal, vol
                    rec["issue"], rec["pages"] = issue, pages
                elif re.match(r"\s*In\b", rest, re.IGNORECASE):
                    # edited-book chapter: "In Editor..., Book title..."
                    rec["source"] = re.sub(
                        r"^\s*In\b\s*", "", rest, count=1,
                        flags=re.IGNORECASE).strip(" .")
                elif rest_low.startswith(("retrieved", "available")):
                    rec["source"] = rest.strip(" .")
                else:
                    # chapter/publisher text that follows a quoted title
                    rec["source"] = rest.strip(" .,;")
        else:
            # unquoted title: "Title. Journal, vol(iss), pages." -> the title
            # ends at the sentence break just before the journal name.
            jb = _journal_bounds(after)
            if jb:
                prefix, vol, issue, pages = jb
                rec["volume"], rec["issue"], rec["pages"] = vol, issue, pages
                cut = max(prefix.rfind(". "), prefix.rfind(".  "))
                if cut > 0:
                    rec["title"] = prefix[:cut].strip(" .")
                    rec["source"] = prefix[cut:].lstrip(" .")
                else:
                    rec["title"] = prefix.strip(" .")
            else:
                rec["title"] = after.strip(" .,")
        rec["kind"] = _classify(rec, text)
        return rec

    # fallback: only a (year) is present (e.g. unnamed works / conjoined refs)
    if rec["authors"] is None:
        # use the *last* year paren so conjoined "A (2023) & B (2024). \"Title\""
        # entries still find the shared title after the final year.
        yits = list(_YEAR_PAREN.finditer(work))
        ym = yits[-1] if yits else None
        if ym and rec["year"] is None:
            rec["year"] = int(ym.group(1))
            after = work[ym.end():].strip(" .")
            title, rest = _split_quoted(after)
            if title:
                rec["title"] = title
                rec["source"] = (rest or None)
            else:
                head = after.split(".", 1)[0].strip(" .,")
                rec["title"] = head
                remainder = after[len(head):].lstrip(" .")
                rec["source"] = (remainder or None)
        elif rec["url"] and re.search(
                r"\b(retrieved from|available at|accessed)\b", work,
                re.IGNORECASE):
            # web-style listing: "Title. Organisation. Retrieved from URL"
            head = re.split(r"\b(retrieved from|available at|accessed)\b",
                            work, maxsplit=1, flags=re.IGNORECASE)[0]
            pieces = [p for p in re.split(r"\.\s+", head) if p.strip()]
            if pieces:
                rec["title"] = pieces[0].strip(" .")
                if len(pieces) > 1:
                    rec["source"] = " ".join(pieces[1:]).strip(" .")
        rec["kind"] = _classify(rec, text)
    if no_author:
        rec["note"] = (rec["note"] + "; [no author given]").strip("; ")
    return rec


def _strong_entry_start(text):
    """Is this line very likely the start of a new bibliography entry?"""
    s = text.strip()
    if not s:
        return False
    if _NUMBERED.match(s):
        return True
    m = _APA_START.match(s)
    return bool(m and m.group("end") == ".")


def _looks_bibliographic(text):
    """Does this text carry any signal that it is a real bibliographic entry
    (year, quoted title, URL/DOI, or 'Retrieved from') rather than prose or a
    numbered instruction?"""
    return bool(
        _YEAR_PAREN.search(text)
        or _QUOTED_TITLE.search(text)
        or _BARE_URL.search(text)
        or _DOI_RAW.search(text)
        or re.search(r"\b(retrieved from|available at|accessed)\b", text,
                     re.IGNORECASE)
    )


# ---------------------------------------------------------------------------
# Collecting everything into records
# ---------------------------------------------------------------------------
def _indent_of(text):
    return len(text) - len(text.lstrip(" "))


def scan_references_segment(seg):
    """Parse a whole references/bibliography region into raw entry texts.
    Entries are split at blank lines and at strong entry starters whose
    indentation is not deeper than the current entry (wrapped continuation
    lines are indented in hanging-indent reference lists and stay with their
    entry)."""
    entries = []          # list of joined raw texts
    cur = []
    cur_indent = None
    for _num, text in seg["lines"]:
        s = text.strip()
        if not s:
            if cur:
                entries.append(" ".join(x.strip() for x in cur))
                cur = []
                cur_indent = None
            continue
        indent = _indent_of(text)
        if _strong_entry_start(text) and (cur_indent is None
                                          or indent <= cur_indent):
            if cur:
                entries.append(" ".join(x.strip() for x in cur))
            cur = [text]
            cur_indent = indent
        else:
            cur.append(text)
    if cur:
        entries.append(" ".join(x.strip() for x in cur))
    return entries


_IMG_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                  ".ico", ".bmp", ".mp4", ".mov", ".webm", ".zip")


def _is_asset_url(url):
    """True for image/media/asset-CDN links that are not published sources."""
    low = url.lower()
    if "user-attachments" in low or "raw.githubusercontent.com" in low:
        return True
    path = low.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path.endswith(_IMG_ASSET_EXTS)


def flow(segment_lines):
    """Join a prose segment into one string, tracking line numbers."""
    buf = []
    offmap = []
    off = 0
    for num, text in segment_lines:
        if not text.strip():
            buf.append(" ")
            offmap.append((off, num))
            off += 1
            continue
        buf.append(text)
        offmap.append((off, num))
        off += len(text)
        buf.append(" ")
        off += 1
    return "".join(buf), offmap


def scan_prose_segment(seg, logger):
    """
    Scan normal body text for inline citations, narrative citations and
    linked / embedded identifiers. Returns (mini_records, raw_entries).
    """
    text, offmap = flow(seg["lines"])
    mini = []
    raw_entries = []

    # Formal entries accidentally living in prose (no "References" heading).
    for idx, (num, line) in enumerate(seg["lines"]):
        if _strong_entry_start(line) and _looks_bibliographic(line):
            # gather following wrapped lines
            block = [line]
            for (n2, l2) in seg["lines"][idx + 1:]:
                if _strong_entry_start(l2) or not l2.strip():
                    break
                block.append(l2)
            raw_entries.append(" ".join(x.strip() for x in block))

    # parenthetical citations
    for m in _PAREN_GROUP.finditer(text):
        core = m.group(1)
        for authors, years in parse_parenthetical(core):
            mini.append({"authors": authors, "years": years,
                         "line": _line_at(offmap, m.start()),
                         "kind": "inline"})

    # narrative citations: "Smith (2020)" / "Smith et al. (2020)"
    for m in _NARRATIVE.finditer(text):
        parsed = parse_narrative(m)
        if parsed:
            authors, years = parsed
            mini.append({"authors": authors, "years": years,
                         "line": _line_at(offmap, m.start()),
                         "kind": "inline"})

    # markdown links + identifiers anywhere
    for label, url in _MD_LINK.findall(text):
        if _is_asset_url(url):
            continue
        mini.append({"kind": "link", "label": label, "url": url,
                     "line": _line_at(offmap, text.find(url))})
    for url in _BARE_URL.findall(text):
        u = url.rstrip(".;:,")
        if _is_asset_url(u):
            continue
        if u and u not in [x["url"] for x in mini if x.get("kind") == "link"]:
            mini.append({"kind": "link", "label": None, "url": u,
                         "line": _line_at(offmap, text.find(u))})
    return mini, raw_entries


def identity(rec):
    """Return a stable identity key for de-duplication."""
    doi = (rec.get("doi") or "").lower()
    if doi:
        return "doi:" + doi.rstrip("/")
    url = (rec.get("url") or "").lower().rstrip("/")
    if url:
        if rec.get("arxiv"):
            return "arxiv:" + rec["arxiv"].lower()
        return "url:" + url
    if rec.get("arxiv"):
        return "arxiv:" + rec["arxiv"].lower()
    t = norm_title(rec.get("title") or "")
    surname = (first_surname(rec.get("authors")) or "").lower()
    if rec.get("year") and t and surname:
        return "work:%s|%s|%s" % (surname, rec["year"], t)
    if t and len(t) >= 3:
        return "title:" + t
    raw = re.sub(r"\s+", " ", (rec.get("raw") or "")).lower()
    return "raw:" + _digest(raw)


# Generic markdown-link labels that are not real titles (never used as one).
_GENERIC_LINK_LABELS = {
    "this article", "this paper", "the paper", "the article", "this study",
    "the study", "the link", "this link", "link", "here", "more",
    "read more", "website", "article", "paper", "source", "reference",
    "full text", "full article", "abstract", "download", "pdf", "details",
    "online", "see", "the source",
}


def link_record(item, heading):
    """Turn a detected hyperlink / identifier into a small record."""
    url = item["url"]
    rec = {"kind": "web", "authors": None, "year": None, "title": None,
           "source": None, "volume": None, "issue": None, "pages": None,
           "doi": None, "url": url, "arxiv": None, "pmid": None,
           "note": None, "raw": url, "line": item.get("line"),
           "heading": heading}
    if "doi.org" in url:
        rec["doi"] = url.split("doi.org/")[-1].rstrip(".)")
    arx = _ARXIV.search(url)
    if arx:
        rec["arxiv"] = arx.group(1)
    else:
        mabs = re.search(
            r"arxiv\.org/(?:abs|pdf)/"
            r"([0-9]{4}\.[0-9]+(?:v[0-9]+)?|"
            r"[a-zA-Z\-]+(\.[A-Z]{1,2})?/[0-9]{4}\.[0-9]+)", url)
        if mabs:
            rec["arxiv"] = mabs.group(1)
    pm = _PMID.search(url)
    if pm:
        rec["pmid"] = pm.group(1)
    label = (item.get("label") or "").strip()
    if label and " " in label and label.lower() not in _GENERIC_LINK_LABELS:
        rec["title"] = label
    rec["kind"] = _classify(rec, url)
    return rec


def collect(path, logger):
    """Run every detector and return the final record list + stats."""
    lines = read_lines(path)
    segments = segment_document(lines)

    full = {}      # identity -> record (from full entries / links)
    inlines = []   # mini inline citation records (author+year only)
    order = []

    def add_full(rec):
        key = identity(rec)
        if rec.get("kind") == "inline":
            return
        if key in full:
            old = full[key]
            # merge line/heading refs and fill gaps
            old["heading"] = old["heading"] or rec.get("heading")
            if not old.get("title") and rec.get("title"):
                old["title"] = rec["title"]
            if not old.get("year") and rec.get("year"):
                old["year"] = rec["year"]
            if not old.get("authors") and rec.get("authors"):
                old["authors"] = rec["authors"]
            for ln in rec.get("lines", []):
                if ln not in old["lines"]:
                    old["lines"].append(ln)
            return
        rec.setdefault("lines", [])
        rec["citations"] = 0
        full[key] = rec
        order.append(key)

    for seg in segments:
        if seg["kind"] == "references":
            for raw in scan_references_segment(seg):
                rec = parse_entry(raw)
                rec["heading"] = seg["heading"]
                rec.setdefault("lines", [])
                add_full(rec)
        else:
            mini, raws = scan_prose_segment(seg, logger)
            for raw in raws:
                rec = parse_entry(raw)
                rec["heading"] = seg["heading"]
                rec.setdefault("lines", [])
                add_full(rec)
            for item in mini:
                if item["kind"] == "inline":
                    inlines.append(item)
                else:
                    add_full(link_record(item, seg["heading"]))

    # --- attach inline citations to their full entries -------------------
    by_sy = {}
    for key in order:
        rec = full[key]
        s = (first_surname(rec.get("authors")) or "").lower()
        if s and rec.get("year"):
            by_sy.setdefault((s, rec["year"]), []).append(rec)

    unresolved = []
    for item in inlines:
        authors = item["authors"]
        s = (first_surname(authors) or "").lower()
        matched = False
        for y in item["years"]:
            for rec in by_sy.get((s, y), []):
                rec["citations"] += 1
                if item.get("line") and item["line"] not in rec.get("lines", []):
                    rec.setdefault("lines", []).append(item["line"])
                matched = True
        if not matched:
            unresolved.append(item)

    # merge duplicate unresolved inline citations
    agg = {}
    for item in unresolved:
        s = (first_surname(item["authors"]) or "").lower()
        key = "inline:%s|%s" % (s, ",".join(str(y) for y in sorted(item["years"])))
        agg.setdefault(key, {"authors": item["authors"],
                             "years": sorted(item["years"]), "count": 0,
                             "lines": []})
        agg[key]["count"] += 1
        if item.get("line"):
            agg[key]["lines"].append(item["line"])

    recs = [full[k] for k in order]
    return recs, agg.values(), segments


# ---------------------------------------------------------------------------
# Completeness / needs-review
# ---------------------------------------------------------------------------
def issues_for(rec):
    problems = []
    kind = rec.get("kind")
    ident = rec.get("doi") or rec.get("url") or rec.get("arxiv") \
        or rec.get("pmid")
    if kind not in ("web", "preprint") and not rec.get("year"):
        problems.append("no year")
    if not rec.get("title") and not ident:
        problems.append("no title/link to identify the work")
    if kind not in ("web", "preprint"):
        if not rec.get("authors") and not rec.get("note"):
            problems.append("no author listed")
    if kind == "other" and not rec.get("title") and not ident:
        problems.append("could not classify the entry")
    if rec.get("note"):
        problems.append("editorial note: %s" % rec["note"])
    return problems


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _render_citation_line(rec):
    """Render one reference as a single text line (no leading numbering)."""
    year = (" (%s)" % rec["year"]) if rec.get("year") else ""
    author = (rec.get("authors") or "").strip()
    title = rec.get("title")
    source = rec.get("source")
    vol = rec.get("volume")
    issue = rec.get("issue")
    pages = rec.get("pages")
    doi, arx, pmid, url = (rec.get(k) for k in
                           ("doi", "arxiv", "pmid", "url"))

    parts = []
    if author:
        parts.append(author + year)
    elif rec.get("year"):
        parts.append("[no author]%s" % year)
    if title:
        if rec.get("kind") in ("journal", "preprint", "conference"):
            parts.append('"%s"' % title)
        else:
            parts.append("*%s*" % title)
    # journal / book / org source (skip a bare URL host when there is no title)
    if source and (title or vol):
        j = source.strip()
        if vol:
            j += ", %s" % vol
        if issue:
            j += "(%s)" % issue
        if pages and not pages.lower().startswith(("e", "art")):
            j += ", %s" % pages
        parts.append(j)
    # identifiers first; a bare URL is only shown when nothing else identifies
    if doi:
        parts.append("doi:%s" % doi)
    if arx:
        parts.append("arXiv:%s" % arx)
    if pmid:
        parts.append("PMID:%s" % pmid)
    if url and not (doi or arx or pmid):
        parts.append(url)
    return " ".join(parts)


def _annotations(rec, show_context):
    bits = []
    if rec.get("citations"):
        bits.append("cited %dx in text" % rec["citations"])
    if show_context and rec.get("heading"):
        bits.append("in section “%s”" % rec["heading"])
    if rec.get("lines"):
        bits.append("lines %s" % ", ".join(str(x) for x in sorted(
            set(rec.get("lines", [])))[:8]))
    return ("   _%s._" % "; ".join(bits)) if bits else None


def _fmt_count(n):
    return "%d source%s" % (n, "" if n == 1 else "s")


def render_markdown(path, recs, unresolved, total_inline, opts):
    lines = []
    title = os.path.basename(path)
    n = len(recs)
    kinds = {}
    for k in _KIND_ORDER:
        kinds[k] = []
    for rec in recs:
        kinds.setdefault(rec["kind"], []).append(rec)
    lines.append("# References extracted from “%s”" % title)
    lines.append("")
    lines.append("_Generated %s — %d distinct %s, %d inline citation"
                 " mention%s._"
                 % (date.today().isoformat(), n, _fmt_count(n),
                    total_inline, "" if total_inline == 1 else "s"))
    lines.append("")
    seen_sections = 0
    for kind in _KIND_ORDER:
        items = kinds.get(kind) or []
        if not items:
            continue
        seen_sections += 1
        lines.append("## %s" % _KIND_LABEL.get(kind, kind))
        lines.append("")
        ordered = items
        if opts.order == "alpha":
            ordered = sorted(items,
                             key=lambda r: (r.get("authors") or "").lower())
        for i, rec in enumerate(ordered, 1):
            lines.append("%d. %s" % (i, _render_citation_line(rec)))
            ann = _annotations(rec, opts.show_context)
            if ann:
                lines.append(ann)
        lines.append("")
    if unresolved:
        lines.append("## Cited inline but no full entry found")
        lines.append("")
        lines.append("_These are referenced in the text but do not match any "
                     "full entry (or appear in no reference list). Check the "
                     "original document._")
        lines.append("")
        for i, u in enumerate(sorted(unresolved,
                                     key=lambda r: (r["authors"].lower())), 1):
            lines.append("%d. (%s, %s) — mentioned %dx"
                         % (i, u["authors"],
                            ", ".join(str(y) for y in u["years"]), u["count"]))
            if opts.show_context and u.get("lines"):
                lines.append("   _lines %s._" % ", ".join(
                    str(x) for x in sorted(set(u["lines"]))[:8]))
        lines.append("")
    return "\n".join(lines) + "\n"


def render_txt(path, recs, unresolved, total_inline, opts):
    md = render_markdown(path, recs, unresolved, total_inline, opts)
    # crude markdown -> text flattening
    out = []
    for ln in md.splitlines():
        if ln.startswith("#"):
            out.append("=" * 4 + " " + ln.lstrip("#").strip() + " " + "=" * 4)
            continue
        if ln.startswith("_") and ln.endswith("_"):
            out.append(ln.strip("_"))
            continue
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        ln = re.sub(r"\*(.+?)\*", r"\1", ln)
        ln = re.sub(r"^(\s*)_(.+?)_\s*$", r"\1\2", ln)  # indented italic notes
        ln = re.sub(r"“|”", '"', ln)
        out.append(ln)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run(args):
    logger = setup_logging()
    if not os.path.exists(args.input):
        logger.error("Input file not found: %s", args.input)
        return 1
    try:
        recs, unresolved, segments = collect(args.input, logger)
    except Exception as exc:  # malformed encoding etc.
        logger.error("Failed to parse %s: %s", args.input, exc)
        return 1

    # drop unresolved/low-confidence records from main list unless flagged
    review = [r for r in recs if issues_for(r)]
    complete = [r for r in recs if not issues_for(r)]

    total_inline = sum(r.get("citations", 0) for r in recs)
    n_unresolved_inline = sum(u["count"] for u in unresolved)
    kinds = {}
    for r in complete:
        kinds.setdefault(r["kind"], 0)
        kinds[r["kind"]] += 1

    logger.info("Scanned %s (%d segments).", args.input, len(segments))
    logger.info("Distinct references found   : %d", len(complete))
    logger.info("  by type: %s", ", ".join(
        "%s=%d" % (_KIND_LABEL.get(k, k), v)
        for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    logger.info("Needs review (incomplete)   : %d", len(review))
    logger.info("Cited inline, no full entry : %d", n_unresolved_inline)
    logger.info("Total inline mentions       : %d", total_inline)

    if args.no_dedupe:
        logger.warning("--no-dedupe is not yet implemented; "
                       "deduplication stayed on.")

    body = (render_markdown(args.input, complete, unresolved, total_inline,
                            args)
            if args.format == "md" else
            render_txt(args.input, complete, unresolved, total_inline, args))

    if review:
        if args.format == "md":
            extra = ["## Possibly incomplete / needs review", ""]
            for r in review:
                head = (r.get("authors") or "[no author]").strip()
                yr = (" (%s)" % r["year"]) if r.get("year") else ""
                extra.append("- **%s%s** — %s" % (head, yr,
                                                  r.get("title")
                                                  or r.get("raw", "")))
                why = "; ".join(issues_for(r)).rstrip(".")
                extra.append("  _%s._" % why)
                if r.get("lines"):
                    extra.append("  _lines %s._" % ", ".join(
                        str(x) for x in sorted(set(r["lines"]))[:8]))
                extra.append("")
            body = (body.rstrip("\n") + "\n\n"
                    + "\n".join(extra).rstrip("\n") + "\n")

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(body)
            logger.info("Report written to %s", args.output)
        except OSError as exc:
            logger.error("Could not write %s: %s", args.output, exc)
            return 1
    else:
        sys.stdout.write(body)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        description="Extract scholarly & published references from a .md or "
                    ".txt research document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(__doc__ or "").split("Usage\n-----\n", 1)[-1],
    )
    p.add_argument("input", help="input file (.md or .txt) to scan")
    p.add_argument("-o", "--output", default=None,
                   help="write the report to this file instead of stdout")
    p.add_argument("--format", choices=["md", "txt"], default=None,
                   help="report format (default: 'md', or 'txt' when the "
                        "output file ends in .txt)")
    p.add_argument("--order", choices=["appearance", "alpha"], default="alpha",
                   help="order within each type section (default: alpha)")
    p.add_argument("--no-context", dest="show_context", action="store_false",
                   help="omit section/line annotations from the report")
    p.add_argument("--no-dedupe", action="store_true",
                   help="reserved: keep every raw hit (not yet implemented)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.format is None:
        if args.output and args.output.lower().endswith(".txt"):
            args.format = "txt"
        else:
            args.format = "md"
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
