"""Tests for `bankruptcy.normalize.classify_debtor`.

The classifier is the central messy-data decision in the pilot: turning a
single bankruptcy filing into (business | individual | unknown) with a
calibrated confidence. The brief explicitly asks us to handle the messy
parts well, so this function is worth pinning down.

Three signals are checked, in priority order:

  1. `chapter == "13"`         → individual, 1.0, "chapter"
  2. docket-entry fingerprint  → individual, 0.95, "docket_fingerprint"
  3. corporate name suffix     → business,   0.8,  "name_suffix"
  4. otherwise                 → unknown,    0.0,  "unmatched"

Tests cover each rule plus the priority-order edge cases (a Chapter 13
case with a corporate suffix still classifies as individual; a Chapter 7
case with both a corporate suffix and a consumer docket fingerprint
trusts the docket fingerprint).
"""

import pytest

from bankruptcy.normalize import classify_debtor


# --- Rule 1: Chapter 13 → individual, 1.0 -----------------------------------

def test_chapter_13_classifies_as_individual_even_with_no_other_signal():
    classification, confidence, method = classify_debtor(
        entity_type="unknown",
        chapter="13",
        recap_documents=[],
    )
    assert classification == "individual"
    assert confidence == 1.0
    assert method == "chapter"


def test_chapter_13_beats_corporate_suffix():
    """A Chapter 13 case with an entity-type suffix should still classify as
    individual — Ch 13 is statutorily individual-only, no exceptions."""
    classification, confidence, method = classify_debtor(
        entity_type="inc",
        chapter="13",
        recap_documents=[],
    )
    assert classification == "individual"
    assert confidence == 1.0
    assert method == "chapter"


# --- Rule 2: docket-entry fingerprint → individual, 0.95 --------------------

@pytest.mark.parametrize("fingerprint_phrase", [
    "Certificate of Credit Counseling",
    "Form 2030",
    "Disclosure of Compensation of Attorney for Debtor",
])
def test_docket_fingerprint_classifies_as_individual(fingerprint_phrase):
    """Each of the three consumer-only fingerprints should fire individually."""
    recap_documents = [{"short_description": fingerprint_phrase}]
    classification, confidence, method = classify_debtor(
        entity_type="unknown",
        chapter="7",
        recap_documents=recap_documents,
    )
    assert classification == "individual"
    assert confidence == 0.95
    assert method == "docket_fingerprint"


def test_docket_fingerprint_matches_case_insensitively():
    """Real CL data has mixed-case `short_description` values; the matcher
    lowercases before comparing."""
    recap_documents = [{"short_description": "CERTIFICATE of credit COUNSELING"}]
    classification, _, method = classify_debtor(
        entity_type="unknown",
        chapter="7",
        recap_documents=recap_documents,
    )
    assert classification == "individual"
    assert method == "docket_fingerprint"


def test_docket_fingerprint_beats_corporate_suffix():
    """A Chapter 7 case whose name has 'Inc' but whose docket carries a
    consumer-only form: trust the docket, not the name. Forms 2030 etc. are
    federal-rule-bound to individual debtors only."""
    recap_documents = [{"short_description": "Form 2030"}]
    classification, confidence, method = classify_debtor(
        entity_type="inc",
        chapter="7",
        recap_documents=recap_documents,
    )
    assert classification == "individual"
    assert confidence == 0.95
    assert method == "docket_fingerprint"


# --- Rule 3: corporate suffix → business, 0.8 -------------------------------

@pytest.mark.parametrize("entity_type", [
    "llc", "inc", "corp", "lp", "llp", "pllc", "pc", "ltd", "co",
])
def test_corporate_entity_types_classify_as_business(entity_type):
    classification, confidence, method = classify_debtor(
        entity_type=entity_type,
        chapter="11",
        recap_documents=[],
    )
    assert classification == "business"
    assert confidence == 0.8
    assert method == "name_suffix"


# --- Rule 4: nothing matches → unknown, 0.0 ---------------------------------

def test_no_signal_at_all_classifies_as_unknown():
    """The conservative fall-through: no Ch 13, no docket fingerprint, no
    detected corporate suffix → `unknown` at confidence 0.0. The pilot
    *does not* guess from name patterns; we surface uncertainty (see
    DECISIONS §6.2)."""
    classification, confidence, method = classify_debtor(
        entity_type="unknown",
        chapter="7",
        recap_documents=[],
    )
    assert classification == "unknown"
    assert confidence == 0.0
    assert method == "unmatched"


def test_empty_chapter_string_with_no_other_signal_is_unknown():
    """Some CourtListener results come with an empty/None chapter. We don't
    classify as Ch 13 (that needs the literal '13')."""
    classification, _, method = classify_debtor(
        entity_type="unknown",
        chapter="",
        recap_documents=[],
    )
    assert classification == "unknown"
    assert method == "unmatched"


def test_recap_documents_without_short_description_dont_crash_or_match():
    """A recap_document with a missing/None short_description shouldn't blow
    up the join logic and shouldn't accidentally count as a fingerprint match."""
    recap_documents = [
        {"short_description": None},
        {"short_description": ""},
        {},  # no key at all
    ]
    classification, _, method = classify_debtor(
        entity_type="unknown",
        chapter="7",
        recap_documents=recap_documents,
    )
    assert classification == "unknown"
    assert method == "unmatched"


def test_recap_documents_concatenated_for_fingerprint_match():
    """Fingerprint check joins all short_descriptions together before
    searching — a fingerprint in any one of N documents should fire."""
    recap_documents = [
        {"short_description": "Voluntary Petition"},
        {"short_description": "Schedules of Assets and Liabilities"},
        {"short_description": "Certificate of Credit Counseling"},  # 3rd entry
        {"short_description": "Statement of Financial Affairs"},
    ]
    classification, _, method = classify_debtor(
        entity_type="unknown",
        chapter="7",
        recap_documents=recap_documents,
    )
    assert classification == "individual"
    assert method == "docket_fingerprint"
