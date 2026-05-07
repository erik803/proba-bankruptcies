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
JOINT_ADMIN_RE = re.compile(r"jointly\s+administered", re.I)
LEAD_CASE_RE = re.compile(r"jointly\s+administered\s+(?:under\s+)?([\d:\-a-z]+)", re.I)


def clean_case_name(raw: str) -> tuple[str, bool, Optional[str]]:
    """Return (cleaned_name, is_joint_administered, lead_case_number_or_None).

    Strips HTML and any trailing "Jointly Administered" phrasing, leaving just
    the debtor name. Also surfaces whether the case carries the joint-admin
    flag and the lead case number it points to (if present).
    """
    text = HTML_TAG_RE.sub(" ", raw).strip()
    text = re.sub(r"\s+", " ", text)

    is_joint = bool(JOINT_ADMIN_RE.search(text))
    lead_match = LEAD_CASE_RE.search(text)
    lead = lead_match.group(1) if lead_match else None

    # Drop the trailing "Jointly Administered..." phrase for the canonical name.
    cleaned = JOINT_ADMIN_RE.split(text, maxsplit=1)[0]
    cleaned = cleaned.rstrip(",. ").strip()
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


def detect_entity_type(name: str) -> str:
    """Map a debtor name's trailing suffix to one of our entity_type values."""
    cleaned = name.strip().rstrip(",.;:")
    for pattern, type_ in ENTITY_SUFFIX_PATTERNS:
        if pattern.search(cleaned):
            return type_
    return "unknown"


def normalize_name(name: str) -> str:
    """Lowercased, suffix-stripped, punctuation-cleaned form for indexed search.

    Used as the `debtor.normalized_name` value, which is the column the API
    hits with `ILIKE '%query%'` (backed by a pg_trgm GIN index).
    """
    s = name.strip()
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


# --- Main mapper ------------------------------------------------------------

COURTLISTENER_BASE = "https://www.courtlistener.com"


def _parse_iso_datetime(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def normalize_courtlistener_result(
    result: dict[str, Any],
) -> tuple[BankruptcyEvent, list[Debtor]]:
    """Map a CourtListener `/api/rest/v4/search/?type=r` result to our domain models.

    Raises ValueError if a critical field (`docket_id`, `dateFiled`) is missing.
    """
    if "docket_id" not in result:
        raise ValueError("result missing docket_id")
    filed_at_str = result.get("dateFiled")
    if not filed_at_str:
        raise ValueError(f"result missing dateFiled (docket_id={result.get('docket_id')})")

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
        "filed_at": date.fromisoformat(filed_at_str),
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
