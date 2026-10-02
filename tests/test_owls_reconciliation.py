from datetime import datetime, timezone

from nhl.market.reconciliation import reconcile_provider_closes
from nhl.market.settlement import SettlementAttestation, SettlementRegistry

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 20, tzinfo=UTC)


def registry(verified=True):
    if not verified: return SettlementRegistry()
    return SettlementRegistry([SettlementAttestation.from_dict({
        "provider": "owls", "sportsbook": "pinnacle", "league": "NHL", "market_type": "ML",
        "settlement_scope": "FULL_GAME_OT_SO", "includes_overtime": True, "includes_shootout": True,
        "evidence_type": "rules", "evidence_reference": "ref", "attested_by": "op",
        "attested_at": "2026-10-01T00:00:00Z", "status": "VERIFIED"})])


def i(price=-115, line=None, book="pinnacle", market="ML"):
    return {"internal_event_id": "2026010001", "sportsbook": book, "market_type": market,
            "selection": "HOME", "line": line, "price": price, "observed_at": "2026-10-02T19:00:00Z", "artifact": "i1"}


def p(price=-115, line=None, book="pinnacle", market="ML", event="owls1"):
    return {"provider_event_id": event, "sportsbook": book, "market_type": market,
            "selection": "HOME", "line": line, "price": price, "observed_at": "2026-10-02T19:01:00Z", "artifact": "p1"}


def run(internal, provider, reg=None, event_map=None):
    return reconcile_provider_closes(internal, provider, event_map or {"owls1": "2026010001"},
                                     {"2026010001": NOW}, reg or registry(), NOW)


def test_exact_price_line_and_both_differences():
    assert run([i()], [p()])[0][0]["reconciliation_status"] == "EXACT"
    assert run([i()], [p(price=-120)])[0][0]["reconciliation_status"] == "PRICE_DIFF"
    # TOTAL is unverified in the helper registry, so use direct ML line values for structural comparison.
    assert run([i(line=1.5)], [p(line=2.5)])[0][0]["reconciliation_status"] == "LINE_DIFF"
    assert run([i(line=1.5)], [p(price=-120, line=2.5)])[0][0]["reconciliation_status"] == "LINE_AND_PRICE_DIFF"


def test_missing_sides_and_unmatched_event():
    assert run([i()], [])[0][0]["reconciliation_status"] == "PROVIDER_CLOSE_MISSING"
    assert run([], [p()])[0][0]["reconciliation_status"] == "INTERNAL_MISSING"
    assert run([], [p(event="bad")], event_map={"x": "2026010001"})[0][0]["reconciliation_status"] == "EVENT_UNMATCHED"


def test_book_market_mismatch_and_unverified_settlement():
    statuses = {r["reconciliation_status"] for r in run([i()], [p(book="other")])[0]}
    assert "BOOK_UNMATCHED" in statuses
    statuses = {r["reconciliation_status"] for r in run([i()], [p(market="TOTAL")])[0]}
    assert "MARKET_UNMATCHED" in statuses or "UNVERIFIED_SETTLEMENT" in statuses
    assert run([i()], [p()], reg=registry(False))[0][0]["reconciliation_status"] == "UNVERIFIED_SETTLEMENT"


def test_reconciliation_is_deterministic_and_never_creates_clv_fields():
    a = run([i()], [p()]); b = run([i()], [p()])
    assert a == b and "clv" not in a[0][0]


def test_ambiguous_provider_close_fails_non_comparable():
    rows, _ = run([i()], [p(), p(price=-120)])
    assert rows[0]["reconciliation_status"] == "NON_COMPARABLE" and not rows[0]["comparable"]
