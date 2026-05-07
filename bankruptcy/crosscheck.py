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

Matching algorithm:
  - Date proximity: |EDGAR.filed_at - CL.filed_at| <= 14 days. Wider than
    the 4-business-day SEC rule because CL latency from PACER can vary.
  - Name similarity: Jaccard >= 0.5 on significant tokens (corporate
    stopwords filtered: 'inc', 'llc', 'group', 'holdings', etc.).
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

# Tokens too common in corporate names to be useful as a match signal.
# Suffix tokens (inc, llc, ...) are usually already stripped by
# normalize_name(), but we list them here as belt-and-braces.
NAME_STOPWORDS = frozenset({
    "the", "and", "of", "for",
    "co", "corp", "corporation", "inc", "incorporated", "ltd", "limited",
    "llc", "lp", "llp", "pllc", "pc",
    "company", "companies",
    "holding", "holdings",
    "group",
    "capital",
    "international", "global", "national", "american",
    "us", "usa", "united", "states",
    "services", "service",
    "enterprises", "enterprise",
    "trust",
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

    primary_debtor_by_event: dict[Any, Debtor] = {}
    for e in (*edgar_events, *cl_events):
        d = session.exec(
            select(Debtor)
            .where(Debtor.event_id == e.event_id)
            .where(Debtor.role == "primary")
        ).first()
        if d:
            primary_debtor_by_event[e.event_id] = d

    matches = 0
    links_made = 0

    for edgar_e in edgar_events:
        edgar_d = primary_debtor_by_event.get(edgar_e.event_id)
        if not edgar_d:
            continue
        edgar_tokens = significant_tokens(edgar_d.normalized_name)
        if not edgar_tokens:
            logger.info(
                "skip edgar %s '%s' — no significant tokens",
                edgar_e.source_record_id,
                edgar_d.name,
            )
            continue

        candidates: list[tuple[float, BankruptcyEvent, Debtor]] = []
        for cl_e in cl_events:
            if abs((cl_e.filed_at - edgar_e.filed_at).days) > DATE_WINDOW_DAYS:
                continue
            cl_d = primary_debtor_by_event.get(cl_e.event_id)
            if not cl_d:
                continue
            score = containment_similarity(
                edgar_tokens, significant_tokens(cl_d.normalized_name)
            )
            if score >= CONTAINMENT_THRESHOLD:
                candidates.append((score, cl_e, cl_d))

        if not candidates:
            logger.info(
                "no match for edgar %s '%s' (tokens=%s)",
                edgar_e.source_record_id,
                edgar_d.name,
                sorted(edgar_tokens),
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
