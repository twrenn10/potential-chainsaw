from datetime import timedelta

import pytest

from nhl.contracts import MarketStatus, MarketType, OddsFormat, OddsSnapshot, Period, Selection, build_market_key
from nhl.data.odds import MARKET_COLUMNS, market_observations_to_csv, parse_market_observations_checked
from nhl.data.snapshots import SnapshotEntry
from nhl.ledger.closes import CloseStore, CloseStoreError
from nhl.market.novig import MalformedMarket, american_to_decimal, decimal_to_american, implied_prob, no_vig
from nhl.market.snapshots import quote_groups, select_closes
from nhl.timeutil import parse_ts

START = parse_ts("2026-10-07T23:00:00Z")
HEAD = ",".join(MARKET_COLUMNS) + "\n"


def entry(fetched="2026-10-08T06:00:00Z"):
    return SnapshotEntry("odds:v2", "odds", "vendor/2026-10-07", parse_ts(fetched), "v2", 1, {})


def row(observed="2026-10-07T22:00:00Z", market="ML", sel="HOME", price="-120", fmt="AMERICAN", line="", team="",
        participant="", status="OPEN", period="", source_ts="", book="BookA", provider="vendorx", game="2026020001"):
    return ",".join([provider, source_ts, observed, game, book, market, period, sel, line, team, participant, price, fmt, status, ""]) + "\n"


def obs(sel, price, ts, status="OPEN", market=MarketType.ML, line=None, book="booka", **kw):
    return OddsSnapshot(ts, book, "2026020001", market, sel, price, line=line, market_status=status, provider="v", **kw)


# 1-2. parser contract + malformed rejection
def test_market_v2_contract_round_trip_and_rejections():
    good = row() + row(sel="AWAY", price="2.05", fmt="DECIMAL", source_ts="2026-10-07T21:59:00Z")
    bad = [
        row(price="-120.5"),                                  # non-integer American
        row(price="50"),                                      # impossible American
        row(price="0.95", fmt="DECIMAL"),                     # impossible decimal
        row(fmt="FRACTIONAL"),                                # unsupported format
        row(observed="2026-10-07T22:00:00"),                  # no UTC offset
        row(observed="2026-10-09T00:00:00Z"),                 # observed after fetch
        row(source_ts="2026-10-07T22:30:00Z"),                # source_ts after observation
        row(market="TOTAL", sel="OVER", line="6.3"),          # line not on 0.5 grid
        row(market="TOTAL", sel="HOME", line="6.5"),          # impossible selection
        row(market="PUCK_LINE", line="0"),                    # impossible puck line
        row(market="ML", line="1.5"),                         # ML takes no line
        row(market="GOALIE_SAVES", sel="OVER", line="27.5"),  # prop without participant
        row(market="TEAM_TOTAL", sel="OVER", line="2.5"),     # team total without team
        row(status="HALTED"),                                 # unknown status
        row(provider=""),                                     # no provider identity
    ]
    res = parse_market_observations_checked((HEAD + good + "".join(bad)).encode(), entry())
    assert len(res.records) == 2 and len(res.rejections) == len(bad)
    away = next(r for r in res.records if r.selection is Selection.AWAY)
    assert away.odds_format is OddsFormat.DECIMAL and away.decimal == 2.05 and away.price_american == 105
    assert away.source_ts == parse_ts("2026-10-07T21:59:00Z") and away.provenance.fetched_at == parse_ts("2026-10-08T06:00:00Z")
    again = parse_market_observations_checked(market_observations_to_csv(res.records).encode(), entry())
    assert [r.to_row() for r in again.records] == [r.to_row() for r in res.records]


def test_duplicated_selection_conflict_is_rejected():
    res = parse_market_observations_checked((HEAD + row() + row() + row(price="-125")).encode(), entry())
    assert res.dupes == 1 and res.records == [] and "conflicting" in res.rejections[0].reason


# 3. deterministic market keys
def test_market_keys_are_deterministic_and_distinct():
    k = build_market_key("2026020001", MarketType.TOTAL, Selection.OVER, 6.5)
    assert k == "2026020001:TOTAL:OVER:+6.5"
    assert build_market_key("2026020001", MarketType.TOTAL, Selection.OVER, 1.5, period=Period.P1) == "2026020001:TOTAL@P1:OVER:+1.5"
    saves = build_market_key("2026020001", MarketType.GOALIE_SAVES, Selection.OVER, 27.5, participant="8479361")
    sog = build_market_key("2026020001", MarketType.PLAYER_SOG, Selection.OVER, 27.5 - 24, participant="8479361")
    assert saves == "2026020001:GOALIE_SAVES:P8479361:OVER:+27.5" and saves != sog
    assert build_market_key("2026020001", MarketType.ML, Selection.HOME) != build_market_key("2026020001", MarketType.REG_3WAY, Selection.HOME)
    with pytest.raises(ValueError):
        build_market_key("2026020001", MarketType.ML, Selection.HOME, 1.5)


# 4. American / decimal conversion
@pytest.mark.parametrize("american,dec", [(-200, 1.5), (-110, 1.9090909), (100, 2.0), (150, 2.5), (-100, 2.0)])
def test_american_decimal(american, dec):
    assert american_to_decimal(american) == pytest.approx(dec, abs=1e-6)
    assert implied_prob(american) == pytest.approx(1 / dec, abs=1e-6)
    assert decimal_to_american(american_to_decimal(american)) in (american, 100 if american == -100 else american)
    with pytest.raises(ValueError):
        american_to_decimal(-99)


# 5-6. no-vig two-way and three-way (favourite/underdog)
def test_no_vig_two_way_keeps_raw_separate():
    r = no_vig(["HOME", "AWAY"], [american_to_decimal(-150), american_to_decimal(130)])
    assert r.raw("HOME") == pytest.approx(0.6) and r.raw("AWAY") == pytest.approx(100 / 230)
    assert sum(r.no_vig) == pytest.approx(1.0) and r.overround == pytest.approx(0.6 + 100 / 230 - 1)
    assert r.fair("HOME") == pytest.approx(0.6 / (0.6 + 100 / 230)) and r.fair("HOME") < r.raw("HOME")
    p = no_vig(["HOME", "AWAY"], [1.25, 4.0], method="power")
    assert sum(p.no_vig) == pytest.approx(1.0) and p.k is not None
    # Power shades the longshot more than multiplicative.
    m = no_vig(["HOME", "AWAY"], [1.25, 4.0])
    assert p.fair("AWAY") < m.fair("AWAY")


def test_no_vig_three_way():
    r = no_vig(["HOME", "DRAW", "AWAY"], [2.3, 4.1, 3.0])
    assert sum(r.no_vig) == pytest.approx(1.0) and len(r.raw_implied) == 3
    assert r.overround == pytest.approx(1 / 2.3 + 1 / 4.1 + 1 / 3.0 - 1)


# 7. incomplete / malformed markets
def test_malformed_markets_rejected():
    for sels, decs in ((["HOME"], [1.9]), (["HOME", "HOME"], [1.9, 1.9]), (["OVER", "UNDER"], [1.0, 3.0]),
                       (["HOME", "AWAY"], [2.2, 2.2])):  # last: negative overround (arb/stale)
        with pytest.raises(MalformedMarket):
            no_vig(sels, decs)
    with pytest.raises(MalformedMarket):
        no_vig(["OVER", "UNDER"], [1.5, 1.5], max_overround=0.15)
    t = START - timedelta(hours=2)
    quotes, rejects = quote_groups([obs(Selection.HOME, -120, t)], START)
    assert not quotes and rejects[0].reason.startswith("INCOMPLETE")
    three = [obs(Selection.HOME, 150, t, market=MarketType.REG_3WAY), obs(Selection.AWAY, 200, t, market=MarketType.REG_3WAY)]
    assert quote_groups(three, START)[1][0].reason == "INCOMPLETE:missing DRAW"
    skew = [obs(Selection.HOME, -120, t), obs(Selection.AWAY, 100, t - timedelta(minutes=30))]
    assert quote_groups(skew, START)[1][0].reason == "LEGS_NOT_CONTEMPORANEOUS"


def _ml(ts, h=-120, a=100, status="OPEN"):
    return [obs(Selection.HOME, h, ts, status), obs(Selection.AWAY, a, ts, status)]


# 8-11. close selection
def test_close_is_last_valid_state_before_actual_puck_drop():
    odds = _ml(START - timedelta(hours=3)) + _ml(START - timedelta(minutes=10), -140, 120) + _ml(START + timedelta(minutes=4), -160, 140)
    actual = START + timedelta(minutes=7)
    c = select_closes(odds, START, actual)[0]
    assert c.available and c.cutoff_basis == "ACTUAL" and c.cutoff == actual
    assert c.quote.prices[Selection.HOME] == -160  # observed after scheduled time but before actual drop
    fb = select_closes(odds, START)[0]
    assert fb.cutoff_basis == "SCHEDULED_FALLBACK" and fb.quote.prices[Selection.HOME] == -140


def test_stale_suspended_or_incomplete_close_is_unavailable_not_fabricated():
    stale = select_closes(_ml(START - timedelta(hours=5)), START)[0]
    assert not stale.available and stale.reason.startswith("STALE") and stale.quote is None
    susp = _ml(START - timedelta(minutes=30)) + [obs(Selection.HOME, -130, START - timedelta(minutes=5), "SUSPENDED")]
    c = select_closes(susp, START)[0]
    assert not c.available and c.reason == "SUSPENDED:HOME"  # the earlier valid quote is NOT substituted
    one_leg = _ml(START - timedelta(minutes=30))[:1]
    assert select_closes(one_leg, START)[0].reason.startswith("INCOMPLETE")


def test_close_store_is_append_only_and_idempotent(tmp_path):
    store = CloseStore(tmp_path / "c.sqlite")
    closes = select_closes(_ml(START - timedelta(minutes=10)) + _ml(START - timedelta(hours=6)), START)
    assert store.append(closes, "2026-10-08T00:00:00Z")["inserted"] == 1
    assert store.append(closes, "2026-10-08T01:00:00Z")["skipped_identical"] == 1
    r = store.rows()[0]
    assert r["cutoff_basis"] == "SCHEDULED_FALLBACK" and r["rule_version"] == "close-v1" and r["status"] == "AVAILABLE"
    different = select_closes(_ml(START - timedelta(minutes=3), -300, 250), START)
    with pytest.raises(CloseStoreError):
        store.append(different, "2026-10-08T02:00:00Z")
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        store.conn.execute("DELETE FROM closes")
    assert store.verify_chain()[0]


def test_props_are_structurally_supported_but_distinct():
    t = START - timedelta(minutes=20)
    props = [obs(Selection.OVER, -115, t, market=MarketType.GOALIE_SAVES, line=27.5, participant="8479361"),
             obs(Selection.UNDER, -105, t, market=MarketType.GOALIE_SAVES, line=27.5, participant="8479361"),
             obs(Selection.OVER, -115, t, market=MarketType.PLAYER_SOG, line=3.5, participant="8479318"),
             obs(Selection.UNDER, -105, t, market=MarketType.PLAYER_SOG, line=3.5, participant="8479318")]
    quotes, _ = quote_groups(props, START)
    assert {q.key.market_id for q in quotes} == {"2026020001:GOALIE_SAVES:P8479361:+27.5", "2026020001:PLAYER_SOG:P8479318:+3.5"}
    assert all(not q.key.market.priced for q in quotes)
    assert MarketStatus.SUSPENDED.value == "SUSPENDED"
