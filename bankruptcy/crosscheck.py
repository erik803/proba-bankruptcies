"""Cross-check pass: link EDGAR public-company filings to CourtListener dockets.

For each EDGAR Item 1.03 8-K, find any CourtListener bankruptcy dockets filed
by the same (or related) entity within ±14 days. When a match is found:

  - Link the EDGAR event into a shared `related_filing_group_id` with the
    matched CourtListener events. If the CL events already form a group
    (e.g. QVC's 17-entity Texas filing), adopt that group_id so we don't
    fragment it.
  - Boost the matched CourtListener events' `classification_confidence` to
    1.0 with method='cross_check'. A public-company SEC disclosure is
    near-definitive evidence the debtor is a business — overrides any
    weaker name-suffix or docket-fingerprint signal.
  - Backfill the EDGAR event's `jurisdiction_court_id` and court name from
    the highest-scoring CL match. EDGAR doesn't tell us which bankruptcy
    court without parsing the 8-K body; cross-check supplies it for free.
  - Copy CIK/ticker identifiers from the EDGAR debtor into the matched CL
    primary debtor's `identifiers` JSONB. Useful for downstream consumers
    who want to pivot from a CL docket to the public-company filings.

Idempotent: only EDGAR events without a group are considered, so re-running
on cross-checked data is a no-op.

Matching algorithm — two passes in priority order:

  1. **Case-number match** (high precision, no date window). When the EDGAR
     event has both a `jurisdiction_court_id` and a `case_number` (both
     extracted from the 8-K body), look for a CL event with the same
     `(court_id, case_number)`. Federal case numbers are unique inside a
     court, so this is essentially a primary-key match. Skips the date
     window entirely — Luminar's 8-K was filed Apr 2026 but their actual
     bankruptcy docket is from Dec 2025; case number is the only signal
     that survives that gap.
  2. **Name + date match** (fallback). For EDGAR events without a case
     number or whose case-number lookup misses, fall back to:
     - Date proximity: |EDGAR.filed_at - CL.filed_at| <= 14 days. Wider
       than the 4-business-day SEC rule because CL latency from PACER
       can vary.
     - Name similarity: containment >= 1.0 on significant tokens
       (corporate stopwords like 'inc', 'llc', 'group', 'holdings'
       filtered out). Containment = |A ∩ B| / min(|A|, |B|); threshold
       1.0 means one token set must be a strict subset of the other.
       Chosen over Jaccard because cross-source name matching is
       fundamentally asymmetric — EDGAR has the parent name (few tokens)
       and CL has the subsidiary names (more tokens), and Jaccard
       penalizes that asymmetry.
"""

import logging
import re
from collections import defaultdict
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from bankruptcy.db import engine
from bankruptcy.models import BankruptcyEvent, Debtor

logger = logging.getLogger("bankruptcy.crosscheck")

DATE_WINDOW_DAYS = 14
# Containment threshold: |A∩B| / min(|A|, |B|). 1.0 means one token set must be
# a strict subset of the other — appropriate for parent-vs-subsidiary matching
# where EDGAR has the parent's name (few tokens) and CL has the subsidiary's
# (more tokens). Jaccard would penalize this asymmetry; containment handles it
# naturally. Threshold 1.0 keeps false positives near zero — partial matches
# would catch e.g. "Cumulus Industries" against "Cumulus Media" which we don't
# want.
CONTAINMENT_THRESHOLD = 1.0

# Tokens too generic to be a match signal. Two groups: legal boilerplate
# (llc, inc, ...) and category words (properties, media, industries, ...).
# Category words were added after a false positive at 77-court scale —
# EDGAR's "OFFICE PROPERTIES INCOME TRUST" matched CL's "W/L Properties
# L.L.C" purely on the shared token "properties". See DECISIONS §8.6.
NAME_STOPWORDS = frozenset({
    # Connectors
    "the", "and", "of", "for",
    # Legal entity suffixes
    "co", "corp", "corporation", "inc", "incorporated", "ltd", "limited",
    "llc", "lp", "llp", "pllc", "pc",
    "company", "companies",
    "holding", "holdings",
    "group",
    "capital",
    "trust",
    # Geography-as-marketing
    "international", "global", "national", "american",
    "us", "usa", "united", "states",
    # Generic activity / service words
    "services", "service",
    "enterprises", "enterprise",
    # Generic category / sector words (added after the OPI/W-L false positive)
    "properties", "property",
    "realty", "estate",
    "media",
    "industries", "industry",
    "technologies", "technology", "tech",
    "solutions", "solution",
    "partners", "ventures",
    "energy", "financial", "healthcare", "pharma",
})


def significant_tokens(name: str) -> frozenset[str]:
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    return frozenset(t for t in tokens if len(t) >= 2 and t not in NAME_STOPWORDS)


def containment_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Return |A∩B| / min(|A|, |B|).

    1.0 iff one set is fully contained in the other — the natural shape for
    matching a parent-company name (short token set) against subsidiaries
    (longer token sets that include the parent name as a substring).
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def crosscheck(session: Session) -> tuple[int, int]:
    """Match EDGAR events to CourtListener events. Returns (matches, links_made)."""

    edgar_events = session.exec(
        select(BankruptcyEvent)
        .where(BankruptcyEvent.source == "edgar")
        .where(BankruptcyEvent.related_filing_group_id.is_(None))
    ).all()

    cl_events = session.exec(
        select(BankruptcyEvent).where(BankruptcyEvent.source == "courtlistener")
    ).all()

    # Fetch all primary debtors in one query, then index by event_id.
    # Avoids N+1 round-trips when the event set grows (every event has at
    # most one primary debtor by schema).
    event_ids = [e.event_id for e in (*edgar_events, *cl_events)]
    primary_debtor_by_event: dict[Any, Debtor] = {
        d.event_id: d
        for d in session.exec(
            select(Debtor)
            .where(Debtor.event_id.in_(event_ids))
            .where(Debtor.role == "primary")
        ).all()
    }

    # Precompute CL token sets once. Without this we'd recompute the same
    # token regex for every (edgar, cl) pair — O(|EDGAR| · |CL|) regex calls.
    cl_tokens_by_event: dict[Any, frozenset[str]] = {
        cl_e.event_id: significant_tokens(
            primary_debtor_by_event[cl_e.event_id].normalized_name
        )
        for cl_e in cl_events
        if cl_e.event_id in primary_debtor_by_event
    }

    # Lookup table for the case-number fast-path: (court_id, case_number) → CL event.
    # Federal bankruptcy case numbers are unique within a court, so this is
    # effectively a primary-key index. Only populate for CL events where both
    # fields are present (case_number is NOT NULL on bankruptcy dockets by
    # convention but worth defending against bad data).
    cl_by_court_case: dict[tuple[str, str], BankruptcyEvent] = {}
    for cl_e in cl_events:
        if cl_e.jurisdiction_court_id and cl_e.case_number:
            cl_by_court_case[(cl_e.jurisdiction_court_id, cl_e.case_number)] = cl_e

    matches = 0
    links_made = 0

    for edgar_e in edgar_events:
        edgar_d = primary_debtor_by_event.get(edgar_e.event_id)
        if not edgar_d:
            continue

        candidates: list[tuple[float, BankruptcyEvent, Debtor]] = []

        # Pass 1: case-number match. Skips the date window entirely because
        # 8-K disclosure date and docket filing date can diverge (Luminar's
        # docket is Dec 2025 but the disclosing 8-K is Apr 2026).
        if edgar_e.jurisdiction_court_id and edgar_e.case_number:
            cn_match = cl_by_court_case.get(
                (edgar_e.jurisdiction_court_id, edgar_e.case_number)
            )
            if cn_match and cn_match.event_id in primary_debtor_by_event:
                # Confidence 2.0 to mark "stronger than any name match" in
                # the sort below — the score is internal-only and doesn't
                # persist anywhere.
                candidates.append(
                    (2.0, cn_match, primary_debtor_by_event[cn_match.event_id])
                )
                logger.info(
                    "case# match | edgar %s case=%s court=%s -> CL %s",
                    edgar_e.source_record_id,
                    edgar_e.case_number,
                    edgar_e.jurisdiction_court_id,
                    cn_match.source_record_id,
                )

        # Pass 2: name + date match. Always run — even when case-number
        # matched, name matching may pick up *additional* subsidiary dockets
        # in the same corporate group (e.g. QVC has 17 subsidiaries on
        # separate case numbers, but only the parent is in the 8-K).
        edgar_tokens = significant_tokens(edgar_d.normalized_name)
        if edgar_tokens:
            for cl_e in cl_events:
                if abs((cl_e.filed_at - edgar_e.filed_at).days) > DATE_WINDOW_DAYS:
                    continue
                cl_tokens = cl_tokens_by_event.get(cl_e.event_id)
                if not cl_tokens:
                    continue
                score = containment_similarity(edgar_tokens, cl_tokens)
                if score >= CONTAINMENT_THRESHOLD:
                    # Avoid duplicating the case# match if name+date hits it too.
                    if any(c[1].event_id == cl_e.event_id for c in candidates):
                        continue
                    candidates.append(
                        (score, cl_e, primary_debtor_by_event[cl_e.event_id])
                    )

        if not candidates:
            logger.info(
                "no match for edgar %s '%s' (tokens=%s, case=%s court=%s)",
                edgar_e.source_record_id,
                edgar_d.name,
                sorted(edgar_tokens) if edgar_tokens else [],
                edgar_e.case_number,
                edgar_e.jurisdiction_court_id,
            )
            continue

        candidates.sort(key=lambda c: -c[0])

        # Choose target group_id: prefer the largest existing CL group; if no
        # candidate has a group yet, mint a fresh one.
        existing_groups: dict[Any, list] = defaultdict(list)
        for c in candidates:
            existing_groups[c[1].related_filing_group_id].append(c)
        non_null = {k: v for k, v in existing_groups.items() if k is not None}
        if non_null:
            target_gid = max(non_null.items(), key=lambda kv: len(kv[1]))[0]
        else:
            target_gid = uuid4()

        # Assign EDGAR event to group; backfill court info from lead match.
        edgar_e.related_filing_group_id = target_gid
        lead_score, lead_cl_e, lead_cl_d = candidates[0]
        if not edgar_e.jurisdiction_court_id and lead_cl_e.jurisdiction_court_id:
            edgar_e.jurisdiction_court_id = lead_cl_e.jurisdiction_court_id
            edgar_e.jurisdiction_court_name = lead_cl_e.jurisdiction_court_name
        session.add(edgar_e)

        # Boost matched CL events' confidence and assign group if needed.
        for score, cl_e, cl_d in candidates:
            if cl_e.classification_confidence < 1.0:
                cl_e.classification_confidence = 1.0
                cl_e.classification_method = "cross_check"
            if cl_e.related_filing_group_id is None:
                cl_e.related_filing_group_id = target_gid
            session.add(cl_e)
            links_made += 1

        # Copy EDGAR's CIK/ticker into the lead CL debtor's identifiers.
        if edgar_d.identifiers:
            merged = dict(lead_cl_d.identifiers or {})
            for k, v in edgar_d.identifiers.items():
                merged.setdefault(k, v)
            lead_cl_d.identifiers = merged  # reassignment so SA marks dirty
            session.add(lead_cl_d)

        logger.info(
            "match | edgar %s '%s' -> group %s | %d CL events, lead=%.2f '%s'",
            edgar_e.source_record_id,
            edgar_d.name[:40],
            str(target_gid)[:8],
            len(candidates),
            lead_score,
            lead_cl_d.name[:40],
        )
        matches += 1

    session.commit()
    return matches, links_made


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    with Session(engine) as session:
        matches, links = crosscheck(session)
    logger.info("Done. matches=%d links=%d", matches, links)


if __name__ == "__main__":
    main()
