from datetime import datetime, timezone

from nhl.market.settlement import SettlementAttestation, SettlementRegistry
from nhl.contracts import Game
from nhl.data.pit import HistoricalStore
from nhl.models.goalie_start import starter_distribution

UTC = timezone.utc
NOW = datetime(2026, 10, 2, tzinfo=UTC)


def att(book="pinnacle", market="ML", status="VERIFIED", scope="FULL_GAME_OT_SO", **extra):
    row = {"provider": "owls", "sportsbook": book, "league": "NHL", "market_type": market,
           "settlement_scope": scope, "includes_overtime": True, "includes_shootout": True,
           "evidence_type": "sportsbook_rules", "evidence_reference": "ref", "attested_by": "operator",
           "attested_at": "2026-10-01T00:00:00Z", "status": status}
    row.update(extra)
    return SettlementAttestation.from_dict(row)


def test_unknown_book_defaults_unverified():
    d = SettlementRegistry().decision("owls", "unknown", "NHL", "ML", NOW)
    assert not d.compatible and d.reason == "UNVERIFIED_SETTLEMENT"


def test_verified_isolated_to_exact_market_key():
    registry = SettlementRegistry([att()])
    assert registry.decision("owls", "pinnacle", "NHL", "ML", NOW).compatible
    assert not registry.decision("owls", "pinnacle", "NHL", "TOTAL", NOW).compatible
    assert not registry.decision("owls", "fanduel", "NHL", "ML", NOW).compatible


def test_conflicting_evidence_fails_closed():
    registry = SettlementRegistry([att(), att(scope="REGULATION", includes_overtime=False,
                                                includes_shootout=False, evidence_reference="other")])
    d = registry.decision("owls", "pinnacle", "NHL", "ML", NOW)
    assert d.status == "CONFLICTING" and not d.compatible


def test_expired_evidence_fails_closed():
    registry = SettlementRegistry([att(expires_at="2026-10-01T12:00:00Z")])
    d = registry.decision("owls", "pinnacle", "NHL", "ML", NOW)
    assert d.status == "EXPIRED" and not d.compatible


def test_regulation_attestation_not_compatible_with_full_game_contract():
    registry = SettlementRegistry([att(scope="REGULATION", includes_overtime=False, includes_shootout=False)])
    assert registry.decision("owls", "pinnacle", "NHL", "ML", NOW).reason == "INCOMPATIBLE_SETTLEMENT_SCOPE"


def test_verified_settlement_cannot_override_goalie_causality():
    assert SettlementRegistry([att()]).decision("owls", "pinnacle", "NHL", "ML", NOW).compatible
    game = Game("2026010001", 2026, "PRESEASON", "2026-10-03T00:00:00Z", "WPG", "BOS")
    store = HistoricalStore(); store.games = [game]
    try:
        starter_distribution(store.view(NOW), game, "WPG")
    except ValueError as exc:
        assert "no usable goalie report" in str(exc)
    else:
        raise AssertionError("settlement attestation bypassed the independent goalie gate")
