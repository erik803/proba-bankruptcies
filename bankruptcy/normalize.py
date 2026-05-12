"""Pure functions: source records → BankruptcyEvent + Debtor instances.

No DB or network side effects. Easy to unit-test, easy to call from any
ingestion pipeline. The CourtListener field mapping mirrors what's
documented in SCHEMA.md — keep them in sync if either changes.
"""

import re
from datetime import date, datetime
from typing import Any, Optional

from bankruptcy.models import BankruptcyEvent, Debtor

# CourtListener wraps `caseName` in HTML for jointly-administered cases:
#   "QVC GCH Company, LLC <b><font color=\"red\">Jointly Administered</font></b>"
# Strip the markup before any name parsing.
HTML_TAG_RE = re.compile(r"<[^>]+>")

# Match the "jointly administered" annotation including an optional leading
# `(` and any surrounding whitespace, so the splitter doesn't leave behind
# an orphan paren like "Fitzgibbon Health Services (" when the source text is
# "Fitzgibbon Health Services (JOINTLY ADMINISTERED - ...)".
# `JOINT_ADMIN_DETECT_RE` is the looser pattern used for boolean detection
# (it doesn't care about leading paren); `JOINT_ADMIN_SPLIT_RE` is the
# pattern used to slice the canonical name off the front of the string.
JOINT_ADMIN_DETECT_RE = re.compile(r"jointly\s+administered", re.I)
JOINT_ADMIN_SPLIT_RE = re.compile(r"\s*\(?\s*jointly\s+administered", re.I)
LEAD_CASE_RE = re.compile(r"jointly\s+administered\s+(?:under\s+)?([\d:\-a-z]+)", re.I)

# Match a trailing *unclosed* parenthetical — i.e. an open paren whose
# closing paren is missing entirely. CourtListener occasionally delivers
# names where an annotation was truncated upstream, leaving "ACME, INC (".
# Closed annotations like "ACME Inc. (NJ)" are *not* matched and are kept
# intact (they carry useful information like geographic distinguisher).
TRAILING_UNCLOSED_PAREN_RE = re.compile(r"\s*\([^)]*$")


def clean_case_name(raw: str) -> tuple[str, bool, Optional[str]]:
    """Return (cleaned_name, is_joint_administered, lead_case_number_or_None).

    Strips HTML and any trailing "Jointly Administered" phrasing, leaving just
    the debtor name. Also surfaces whether the case carries the joint-admin
    flag and the lead case number it points to (if present).
    """
    text = HTML_TAG_RE.sub(" ", raw).strip()
    text = re.sub(r"\s+", " ", text)

    is_joint = bool(JOINT_ADMIN_DETECT_RE.search(text))
    lead_match = LEAD_CASE_RE.search(text)
    lead = lead_match.group(1) if lead_match else None

    # Drop the "...Jointly Administered..." trailing annotation, including
    # any leading paren that introduces it. Then mop up trailing punctuation
    # and any unclosed parenthetical CourtListener may have delivered.
    cleaned = JOINT_ADMIN_SPLIT_RE.split(text, maxsplit=1)[0]
    cleaned = cleaned.rstrip(",. ").strip()
    cleaned = TRAILING_UNCLOSED_PAREN_RE.sub("", cleaned).rstrip(",. ").strip()
    return cleaned, is_joint, lead

# --- Entity-name parsing ----------------------------------------------------

# Order matters: longest patterns first so PLLC doesn't get caught by PC, etc.
ENTITY_SUFFIX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bP\.?L\.?L\.?C\.?\s*$", re.I), "pllc"),
    (re.compile(r"\bL\.?L\.?C\.?\s*$", re.I), "llc"),
    (re.compile(r"\bL\.?L\.?P\.?\s*$", re.I), "llp"),
    (re.compile(r"\bL\.?P\.?\s*$", re.I), "lp"),
    (re.compile(r"\bP\.?C\.?\s*$", re.I), "pc"),
    (re.compile(r"\b(?:INC\.?|INCORPORATED)\s*$", re.I), "inc"),
    (re.compile(r"\b(?:CORP\.?|CORPORATION)\s*$", re.I), "corp"),
    (re.compile(r"\b(?:LTD\.?|LIMITED)\s*$", re.I), "ltd"),
    (re.compile(r"\b(?:CO\.?|COMPANY)\s*$", re.I), "co"),
]

# Match a trailing parenthetical that masks an entity suffix from end-of-string
# matching. CourtListener sometimes truncates long case names mid-parenthetical
# (e.g. "THE TRUETT MEMORIAL SOUTHERN BAPTIST CHURCH, INC (") and otherwise
# attaches annotations like "(DEBTOR)" or "(a Delaware Corp)". Stripping these
# before suffix detection lets the underlying INC/LLC/etc. still be picked up.
TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)?\s*$")


def detect_entity_type(name: str) -> str:
    """Map a debtor name's trailing suffix to one of our entity_type values."""
    cleaned = TRAILING_PAREN_RE.sub("", name.strip()).rstrip(",.;:")
    for pattern, type_ in ENTITY_SUFFIX_PATTERNS:
        if pattern.search(cleaned):
            return type_
    return "unknown"


def normalize_name(name: str) -> str:
    """Lowercased, suffix-stripped, punctuation-cleaned form for indexed search.

    Used as the `debtor.normalized_name` value, which is the column the API
    hits with `ILIKE '%query%'` (backed by a pg_trgm GIN index).
    """
    s = TRAILING_PAREN_RE.sub("", name.strip())
    for pattern, _ in ENTITY_SUFFIX_PATTERNS:
        s = pattern.sub("", s).strip()
    s = s.rstrip(",.;:").strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


# --- Classification ---------------------------------------------------------

# These docket-entry descriptions are produced for individual debtors only —
# their presence is a high-confidence signal the case is consumer, not
# business. Their absence is NOT proof of business; corporate cases also
# don't generate these.
INDIVIDUAL_DOCKET_FINGERPRINTS = (
    "certificate of credit counseling",
    "form 2030",
    "disclosure of compensation of attorney for debtor",
)

CORPORATE_ENTITY_TYPES = {"llc", "inc", "corp", "lp", "llp", "pllc", "pc", "ltd", "co"}


def classify_debtor(
    *,
    entity_type: str,
    chapter: str,
    recap_documents: list[dict[str, Any]],
) -> tuple[str, float, str]:
    """Return (classification, confidence, method) per the schema's classification spec."""
    # Chapter 13 is reserved for individuals (wage-earner repayment plans).
    if str(chapter) == "13":
        return ("individual", 1.0, "chapter")

    descriptions = " | ".join(
        (doc.get("short_description") or "").lower() for doc in recap_documents
    )
    for fingerprint in INDIVIDUAL_DOCKET_FINGERPRINTS:
        if fingerprint in descriptions:
            return ("individual", 0.95, "docket_fingerprint")

    if entity_type in CORPORATE_ENTITY_TYPES:
        return ("business", 0.8, "name_suffix")

    return ("unknown", 0.0, "unmatched")


# --- Chapter mapping --------------------------------------------------------

PROCEEDING_TYPE_BY_CHAPTER: dict[str, str] = {
    "7": "chapter_7",
    "11": "chapter_11",
    "13": "chapter_13",
    "15": "chapter_15",
}


def proceeding_type_from_chapter(chapter: Any) -> str:
    if chapter is None or chapter == "":
        return "other"
    return PROCEEDING_TYPE_BY_CHAPTER.get(str(chapter), "other")


# --- 8-K body chapter extraction --------------------------------------------

# Match a "Chapter N" phrase next to a phrase that strongly implies it's
# referring to the bankruptcy filing itself (not a generic Bankruptcy Code
# reference, not a narrative mention of "Chapter 11 process" in boilerplate).
#
# Patterns in priority order — first match wins. Each captures one group:
# the chapter number as a string.
_STRONG_CHAPTER_PATTERNS = [
    # "voluntary petitions for relief under Chapter 11" — the canonical phrasing
    re.compile(
        r"(?:voluntary\s+)?petition[s]?\s+(?:for\s+relief\s+)?(?:under|pursuant\s+to)\s+chapter\s+(7|11|13|15)\b",
        re.I,
    ),
    # "filed a voluntary Chapter 11 case"
    re.compile(
        r"\b(?:filed|commenced)\s+(?:a\s+)?(?:voluntary\s+)?chapter\s+(7|11|13|15)\s+(?:case|petition|proceeding)",
        re.I,
    ),
    # "Chapter 11 of Title 11" / "Chapter 11 of the United States Bankruptcy Code"
    re.compile(
        r"\bchapter\s+(7|11|13|15)\s+of\s+(?:the\s+)?(?:united\s+states\s+)?(?:bankruptcy\s+code|title\s+11)",
        re.I,
    ),
    # "filed for Chapter 11 protection/bankruptcy/relief"
    re.compile(
        r"\bfil(?:ed|ing)\s+for\s+chapter\s+(7|11|13|15)\s+(?:protection|bankruptcy|relief)",
        re.I,
    ),
]

# Anything that strips a tag — minimal HTML scrubber. 8-K HTML is messy but
# the chapter mentions all live in plain narrative paragraphs; a full DOM
# parser is overkill here.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# Match phrases that indicate a NON-federal-bankruptcy insolvency proceeding.
# These trigger Item 1.03 disclosure (which covers "Bankruptcy or Receivership"
# broadly) but should not be classified as chapter_7/11/13/15. Examples in
# the wild: Marizyme filed a Florida Chapter 727 Assignment for the Benefit
# of Creditors — a state-law proceeding, not a federal bankruptcy case.
_NON_FEDERAL_PATTERNS = [
    re.compile(r"\bassignment\s+for\s+the\s+benefit\s+of\s+creditors\b", re.I),
    re.compile(r"\b(?:state-court|state\s+court)\s+receivership\b", re.I),
    re.compile(r"\bappointment\s+of\s+a?\s*receiver\s+(?:by|under)\s+(?:state|the\s+circuit)", re.I),
]


def extract_proceeding_type_from_8k_body(
    body: Optional[str],
) -> tuple[Optional[str], float, str]:
    """Parse an 8-K body for the bankruptcy chapter or non-federal proceeding.

    Returns `(proceeding_type, confidence, method)` where `proceeding_type`
    is a valid schema value:

    - `'chapter_7' / 'chapter_11' / 'chapter_13' / 'chapter_15'` — federal
      bankruptcy case detected
    - `'other'` — Item 1.03 disclosure covers a non-federal proceeding
      (state-law Assignment for the Benefit of Creditors, state-court
      receivership, etc.). These get filed under Item 1.03 because Item 1.03
      reads "Bankruptcy *or Receivership*" — broader than just federal
      bankruptcy.
    - `None` — nothing parsable; caller decides the fallback

    `confidence` reflects how strong the signal was; `method` tags which
    rule fired (for audit / debugging).
    """
    if not body:
        return None, 0.0, "no_body"

    text = _HTML_TAG_RE.sub(" ", body)
    text = _WS_RE.sub(" ", text)

    # 1. Federal chapter — strong patterns (petition + chapter, Title 11, etc.)
    for pattern in _STRONG_CHAPTER_PATTERNS:
        match = pattern.search(text)
        if match:
            chapter = match.group(1)
            return PROCEEDING_TYPE_BY_CHAPTER.get(chapter, "other"), 0.95, "8k_body_strong"

    # 2. Non-federal proceeding (state ABC, state-court receivership)
    for pattern in _NON_FEDERAL_PATTERNS:
        if pattern.search(text):
            return "other", 0.9, "8k_body_state_proceeding"

    # 3. Frequency fallback — many "Chapter N" mentions where one dominates
    weak_matches = re.findall(r"\bchapter\s+(7|11|13|15)\b", text, re.I)
    if weak_matches:
        from collections import Counter

        counts = Counter(weak_matches)
        most_common, count = counts.most_common(1)[0]
        total = sum(counts.values())
        if count >= 2 and count / total >= 0.7:
            return (
                PROCEEDING_TYPE_BY_CHAPTER.get(most_common, "other"),
                0.65,
                "8k_body_frequency",
            )

    return None, 0.0, "8k_body_no_match"


# --- 8-K body court + case-number extraction --------------------------------

# Map the federal-district phrase that appears in 8-K bodies to CourtListener's
# court_id. Covers the courts that handle ~90% of public-company Ch 11 cases
# plus everything else we've seen in our own data. Unknown phrases return the
# extracted court name as `jurisdiction_court_name` with `jurisdiction_court_id`
# left None — cross-check + future runs can still match on the name.
EDGAR_COURT_NAME_TO_CL_ID: dict[str, str] = {
    # Most-used public-company bankruptcy venues
    "district of delaware": "deb",
    "southern district of new york": "nysb",
    "southern district of texas": "txsb",
    "central district of california": "cacb",
    "district of new jersey": "njb",
    "eastern district of virginia": "vaeb",
    "northern district of illinois": "ilnb",
    # Long tail we've actually seen in our own data
    "middle district of florida": "flmb",
    "southern district of florida": "flsb",
    "northern district of california": "canb",
    "eastern district of california": "caeb",
    "southern district of california": "casb",
    "northern district of texas": "txnb",
    "western district of texas": "txwb",
    "eastern district of new york": "nyeb",
    "northern district of georgia": "ganb",
    "eastern district of north carolina": "nceb",
    "western district of washington": "wawb",
    "district of arizona": "arb",  # CL uses 'ar' for Arizona, not the standard 'az'
    "district of colorado": "cob",
    "western district of pennsylvania": "pawb",
    "district of massachusetts": "mab",
    "district of columbia": "dcb",
    "northern district of alabama": "alnb",
    "eastern district of michigan": "mieb",
    "middle district of tennessee": "tnmb",
    "eastern district of tennessee": "tneb",
    "northern district of ohio": "ohnb",
    "southern district of ohio": "ohsb",
    "district of puerto rico": "prb",
    "eastern district of missouri": "moeb",
    "eastern district of oklahoma": "okeb",
    "western district of oklahoma": "okwb",
    "northern district of oklahoma": "oknb",
}

# Match the court phrase that 8-Ks use to introduce the venue. Two shapes:
#   "United States Bankruptcy Court for the Southern District of Texas"
#   "United States Bankruptcy Court for the District of Delaware"
# Capture the district phrase so we can map to CL.
_COURT_PHRASE_RE = re.compile(
    r"United\s+States\s+Bankruptcy\s+Court\s+for\s+the\s+"
    r"((?:Northern|Southern|Eastern|Western|Middle|Central)\s+)?"
    r"District\s+of\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)",
    re.I,
)

# Match a case number like "Case No. 25-90807" or "Case Number 26-10708 (XYZ)".
# Two-digit year prefix, dash, 4-6 digit serial, optional judge initials in
# parens. The (CML) / (KBO) suffix is the assigned judge's initials, not part
# of the case number — strip it from the captured group.
_CASE_NUMBER_RE = re.compile(
    r"Case\s+(?:No\.?|Number)\s*[:\s]*\s*"
    r"(\d{2}-\d{4,6})",  # e.g. 26-90346 or 25-90807
    re.I,
)


def extract_court_and_case_from_8k_body(
    body: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """Parse an 8-K body for the bankruptcy court name + CL court_id + case number.

    Returns `(court_id, court_name, case_number, method)`:

    - `court_id`: CourtListener-style court ID (e.g. "txsb"), or None when
      the body names a court we don't have in our mapping (court_name is
      still returned so consumers see *some* venue info)
    - `court_name`: the canonical "United States Bankruptcy Court..." phrase
      as it appears in the body, or None when no court phrase fires
    - `case_number`: the matched docket like "26-90346", or None
    - `method`: which signals fired, used for audit / debugging. One of
      `8k_body_court_and_case`, `8k_body_court_only`, `8k_body_case_only`,
      `8k_body_no_match`, `no_body`
    """
    if not body:
        return None, None, None, "no_body"

    text = _HTML_TAG_RE.sub(" ", body)
    text = _WS_RE.sub(" ", text)
    # HTML entities (e.g. "&#160;" for non-breaking space) survive tag-strip
    # and break "District&#160;of&#160;Texas". A targeted swap is cheaper
    # than html.unescape on a 60kb body.
    text = text.replace("&#160;", " ").replace("&nbsp;", " ")
    text = _WS_RE.sub(" ", text)

    court_id: Optional[str] = None
    court_name: Optional[str] = None
    court_match = _COURT_PHRASE_RE.search(text)
    if court_match:
        direction = (court_match.group(1) or "").strip().lower()
        state = court_match.group(2).strip()
        # Normalize multi-word states; the mapping key has no extra spaces.
        district_phrase = (
            f"{direction} district of {state}".strip().lower()
            if direction
            else f"district of {state}".lower()
        )
        # Re-canonicalize whitespace so the lookup key matches.
        district_phrase = " ".join(district_phrase.split())
        court_id = EDGAR_COURT_NAME_TO_CL_ID.get(district_phrase)
        # Always return the canonical court name — useful even when court_id
        # lookup misses, since the cross-check can fall back to name matching.
        court_name = (
            f"United States Bankruptcy Court for the "
            f"{direction.title() + ' ' if direction else ''}District of {state}"
        ).strip()

    case_match = _CASE_NUMBER_RE.search(text)
    case_number = case_match.group(1) if case_match else None

    if court_name and case_number:
        method = "8k_body_court_and_case"
    elif court_name:
        method = "8k_body_court_only"
    elif case_number:
        method = "8k_body_case_only"
    else:
        method = "8k_body_no_match"

    return court_id, court_name, case_number, method


# --- Main mapper ------------------------------------------------------------

COURTLISTENER_BASE = "https://www.courtlistener.com"


def _parse_iso_datetime(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- EDGAR mapper -----------------------------------------------------------

# `display_names` items look like:
#   "Luminar Technologies, Inc./DE  (LAZRQ)  (CIK 0001758057)"
#   "MARIZYME, INC.  (CIK 0001413754)"                 <- no ticker
#   "QVC Group, Inc.  (QVCGA, QVCGB, QVCGP)  (CIK 0001104659)"  <- multi-ticker
TICKER_TOKEN = r"[A-Z0-9.\-]+"
EDGAR_DISPLAY_NAME_RE = re.compile(
    rf"^(?P<name>.+?)\s*"
    rf"(?:\((?P<tickers>{TICKER_TOKEN}(?:\s*,\s*{TICKER_TOKEN})*)\)\s*)?"
    rf"\(CIK\s+(?P<cik>\d+)\)\s*$"
)


def parse_edgar_display_name(
    s: str,
) -> tuple[str, list[str], Optional[str]]:
    """Parse an EDGAR display_names entry into (name, tickers, cik).

    `tickers` is a list because issuers with multiple share classes (QVC's
    QVCGA / QVCGB / QVCGP) appear with all of them in one parenthetical.
    """
    m = EDGAR_DISPLAY_NAME_RE.match(s.strip())
    if not m:
        return s.strip(), [], None
    name = m.group("name").strip().rstrip(",.")
    # EDGAR appends a "/XX" state-of-incorporation marker to some names, e.g.
    # "Luminar Technologies, Inc./DE". Strip it so downstream suffix stripping
    # (Inc./Corp.) and tokenization don't pick "de" up as a name token.
    name = re.sub(r"/[A-Z]{2}\s*$", "", name).strip().rstrip(",.")
    tickers_str = m.group("tickers") or ""
    tickers = [t.strip() for t in tickers_str.split(",") if t.strip()]
    return name, tickers, m.group("cik")


def normalize_edgar_filing(
    hit: dict[str, Any],
    body: Optional[str] = None,
) -> tuple[BankruptcyEvent, list[Debtor]]:
    """Map an EDGAR EFTS hit (8-K with Item 1.03) to BankruptcyEvent + Debtor.

    EDGAR records are by definition public-company business filings, so
    classification is set to ('business', 1.0, 'edgar_public_company').
    Court information is left blank — the 8-K body usually names the
    bankruptcy court, but parsing it is out of scope; the cross-check pass
    backfills it from CourtListener when a match is found.

    If `body` is provided, we parse it for the chapter / non-federal
    proceeding type — this distinguishes a real Chapter 11 from a state-law
    Assignment for the Benefit of Creditors (which also files an Item 1.03
    8-K). When `body` is None or parsing yields no signal, we fall back to
    the historical default (`chapter_11`) with reduced confidence — public
    company bankruptcies are overwhelmingly Ch 11 in practice.
    """
    accession = hit.get("adsh")
    if not accession:
        raise ValueError("EDGAR hit missing accession number (adsh)")
    file_date_str = hit.get("file_date")
    if not file_date_str:
        raise ValueError(f"EDGAR hit missing file_date: {accession}")
    display_names = hit.get("display_names") or []
    if not display_names:
        raise ValueError(f"EDGAR hit missing display_names: {accession}")

    name, tickers, cik = parse_edgar_display_name(display_names[0])

    # Construct a stable filing-index URL from the accession number.
    # Format: .../data/{cik_no_leading_zeros}/{accession_no_dashes}/
    accession_no_dashes = accession.replace("-", "")
    cik_no_leading = (cik or "").lstrip("0") or "0"
    source_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_no_leading}/{accession_no_dashes}/"
    )

    address: Optional[dict[str, Any]] = None
    if hit.get("biz_locations"):
        address = {"location": hit["biz_locations"][0]}
        if hit.get("biz_states"):
            address["state"] = hit["biz_states"][0]

    js: dict[str, Any] = {}
    if hit.get("items"):
        js["8k_items"] = list(hit["items"])
    if hit.get("inc_states"):
        js["incorporation_states"] = list(hit["inc_states"])
    if hit.get("sics"):
        js["sic_codes"] = list(hit["sics"])
    if hit.get("period_ending"):
        js["period_ending"] = hit["period_ending"]

    # Parse the 8-K body for the actual proceeding type when we have it.
    # On no body / no signal, default to chapter_11 (public-company
    # bankruptcies are overwhelmingly Ch 11) and record the lower confidence
    # in jurisdiction_specific.
    proceeding_type, pt_confidence, pt_method = extract_proceeding_type_from_8k_body(body)
    if proceeding_type is None:
        proceeding_type = "chapter_11"
        # pt_method already reflects 'no_body' or '8k_body_no_match'; the
        # confidence (0.0) tells consumers this is a default, not a parse.
    js["proceeding_type_method"] = pt_method
    js["proceeding_type_confidence"] = pt_confidence

    # Extract court name + case number from the same body. Falls back to
    # None when the 8-K is a non-federal proceeding (e.g. Marizyme's state
    # ABC) or when our court-name map doesn't recognize the district.
    court_id_from_body, court_name_from_body, case_number_from_body, court_method = (
        extract_court_and_case_from_8k_body(body)
    )
    js["court_extraction_method"] = court_method

    event = BankruptcyEvent(
        source="edgar",
        source_record_id=accession,
        source_url=source_url,
        jurisdiction_country="US",
        # Body parse populates these directly when the 8-K names the venue
        # and case number. Cross-check still acts as a backup to fill these
        # in from a matched CL docket when body extraction misses.
        jurisdiction_court_id=court_id_from_body,
        jurisdiction_court_name=court_name_from_body,
        proceeding_type=proceeding_type,
        case_number=case_number_from_body,
        pacer_case_id=None,
        filed_at=date.fromisoformat(file_date_str),
        source_first_seen_at=None,
        debtor_classification="business",
        classification_confidence=1.0,
        classification_method="edgar_public_company",
        jurisdiction_specific=js,
        raw=hit,
    )

    identifiers: dict[str, Any] = {}
    if cik:
        identifiers["cik"] = cik
    if tickers:
        identifiers["tickers"] = tickers

    debtor = Debtor(
        event_id=event.event_id,
        name=name,
        normalized_name=normalize_name(name),
        entity_type=detect_entity_type(name),
        role="primary",
        identifiers=identifiers,
        address=address,
    )

    return event, [debtor]


# --- CourtListener mapper ---------------------------------------------------


def normalize_courtlistener_result(
    result: dict[str, Any],
) -> tuple[BankruptcyEvent, list[Debtor]]:
    """Map a CourtListener `/api/rest/v4/search/?type=r` result to our domain models.

    Raises ValueError if a critical field (`docket_id`, `dateFiled`) is missing,
    or if the row is one of CourtListener's known garbage shapes:
      - `caseName == "Miscellaneous Entry"` — PACER administrative placeholders
        (40 of these landed in a single ingest with synthetic `dateFiled` of
        2029-01-01, all named identically; they're not real bankruptcies)
      - `dateFiled` in the implausible future (years > today + 1) — data entry
        typos at the court level (we saw e.g. `2079-11-23` for a 2021 filing)

    Both are filtered at ingest rather than handled downstream because they
    pollute aggregates (max-date, chart axes, watermark) in ways that are
    expensive to fix once they're in the DB.
    """
    if "docket_id" not in result:
        raise ValueError("result missing docket_id")
    filed_at_str = result.get("dateFiled")
    if not filed_at_str:
        raise ValueError(f"result missing dateFiled (docket_id={result.get('docket_id')})")

    # Skip PACER administrative placeholders. CL exposes these via the search
    # API but they're not real cases — they're bulk-entry artifacts.
    if (result.get("caseName") or "").strip().lower() == "miscellaneous entry":
        raise ValueError(
            f"pacer placeholder 'Miscellaneous Entry' (docket_id={result.get('docket_id')})"
        )

    # Reject implausible future filed_at — typo in the upstream record.
    parsed_filed = date.fromisoformat(filed_at_str)
    if parsed_filed.year > date.today().year + 1:
        raise ValueError(
            f"implausible future dateFiled={filed_at_str} "
            f"(docket_id={result.get('docket_id')})"
        )

    # `parsed_filed` already validated above.
    chapter_raw = result.get("chapter")
    chapter = str(chapter_raw) if chapter_raw is not None else ""

    # `caseName` is the canonical reference for the primary debtor. We strip
    # HTML markup that CourtListener injects for jointly-administered cases
    # ("<b><font color='red'>Jointly Administered</font></b>") and surface the
    # joint-admin flag separately into jurisdiction_specific. The `party`
    # array isn't reliable here — it sometimes lists procedural participants
    # (e.g. "U.S. Trustee") and isn't ordered debtor-first.
    raw_case_name = result.get("caseName") or ""
    case_name, is_joint_admin, lead_case_number = clean_case_name(raw_case_name)
    parties: list[str] = [case_name] if case_name else []

    # Build the event first so we have its event_id for FK back-reference.
    event_kwargs: dict[str, Any] = {
        "source": "courtlistener",
        "source_record_id": str(result["docket_id"]),
        "jurisdiction_country": "US",
        "jurisdiction_court_id": result.get("court_id") or "",
        "jurisdiction_court_name": result.get("court"),
        "proceeding_type": proceeding_type_from_chapter(chapter),
        "case_number": result.get("docketNumber"),
        "pacer_case_id": result.get("pacer_case_id"),
        "filed_at": parsed_filed,
        "source_first_seen_at": _parse_iso_datetime(
            (result.get("meta") or {}).get("date_created")
        ),
        "raw": result,
    }

    docket_url = result.get("docket_absolute_url")
    if docket_url:
        event_kwargs["source_url"] = f"{COURTLISTENER_BASE}{docket_url}"

    # jurisdiction_specific sidecar — drop None values for cleanliness.
    js: dict[str, Any] = {}
    if result.get("assignedTo"):
        js["judge"] = result["assignedTo"]
    if result.get("trustee_str"):
        js["trustee"] = result["trustee_str"]
    docket_entries = [
        {
            "date": doc.get("entry_date_filed"),
            "description": doc.get("short_description"),
            "doc_id": doc.get("pacer_doc_id") or None,
            "entry_number": doc.get("entry_number"),
        }
        for doc in (result.get("recap_documents") or [])
        if doc.get("short_description")
    ]
    if docket_entries:
        js["docket_entries"] = docket_entries
    if is_joint_admin:
        js["joint_administration"] = True
        if lead_case_number:
            js["lead_case_number"] = lead_case_number
    event_kwargs["jurisdiction_specific"] = js

    # Classification depends on the primary debtor's entity type + docket entries.
    primary_entity_type = detect_entity_type(parties[0]) if parties else "unknown"
    classification, confidence, method = classify_debtor(
        entity_type=primary_entity_type,
        chapter=chapter,
        recap_documents=result.get("recap_documents") or [],
    )
    event_kwargs["debtor_classification"] = classification
    event_kwargs["classification_confidence"] = confidence
    event_kwargs["classification_method"] = method

    event = BankruptcyEvent(**event_kwargs)

    debtors: list[Debtor] = [
        Debtor(
            event_id=event.event_id,
            name=name,
            normalized_name=normalize_name(name),
            entity_type=detect_entity_type(name),
            role="primary" if i == 0 else "co_debtor",
        )
        for i, name in enumerate(parties)
    ]

    return event, debtors
