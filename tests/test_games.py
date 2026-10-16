from collector.games.radar import _heat_value, _record_history
from collector.games.sources import appstore_new, from_trending_games, steam_new
from collector.games.store import (
    downsample,
    export_games,
    game_group,
    is_web_game,
    rise_ratio,
    score_series,
)


def test_game_group_trending_vs_listed():
    assert game_group({"name": "whatever", "sources": ["trending:US"]}) == "trending"
    assert game_group({"name": "coolgame.io", "sources": ["steam_new"]}) == "trending"
    assert game_group({"name": "Some Game", "sources": ["steam_new", "appstore:us"]}) == "listed"


def test_heat_value_uses_rank_when_uncharted():
    assert _heat_value({"name": "x", "rank": 1, "size": 10}) == 100
    assert _heat_value({"name": "x", "rank": 10, "size": 10}) == 10
    assert _heat_value({"name": "x", "score": 42, "series": [1, 2, 3]}) == 42


def test_heat_value_falls_back_to_growth():
    assert _heat_value({"name": "x", "growth": 1000}) == 100
    assert _heat_value({"name": "x", "growth": 500}) == 50
    assert _heat_value({"name": "x", "growth": 0}) == 0


def test_export_games_adds_group_and_growth7d():
    state = {"games": {
        "a": {"name": "Trend A", "series": [1, 2, 3], "chart_at": "x", "score": 40,
              "sources": ["trending:US"], "history": [["t0", 10], ["t1", 15]]},
        "b": {"name": "Listed B", "series": [], "chart_at": None, "score": 0,
              "sources": ["steam_new"], "history": [["t0", 50], ["t1", 60]]},
    }}
    by = {i["name"]: i for i in export_games(state, "now", min_peak=0)["items"]}
    assert by["Trend A"]["group"] == "trending"
    assert by["Listed B"]["group"] == "listed"
    assert by["Trend A"]["growth7d"] == 0.5
    assert by["Trend A"]["history"] == [["t0", 10], ["t1", 15]]


def test_record_history_appends_and_prunes():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    games = {"g": {"name": "G", "score": 30, "series": [1, 2, 3], "chart_at": "x",
                   "history": [[(now - timedelta(days=10)).isoformat(), 5]]}}
    _record_history(games, now.isoformat(), days=7)
    hist = games["g"]["history"]
    assert hist[-1][1] == 30   # score is the heat
    assert len(hist) == 1      # the 10-day-old point was pruned


def test_downsample_reduces_and_preserves_shape():
    assert downsample([1, 2, 3, 4], 2) == [2, 4]
    assert downsample([5, 5], 4) == [5, 5]
    out = downsample(list(range(100)), 10)
    assert len(out) == 10


def test_score_series_rewards_momentum():
    flat = score_series([50] * 20)
    rising = score_series([10] * 10 + [90] * 10)
    falling = score_series([90] * 10 + [10] * 10)
    assert rising > flat > falling
    assert score_series([]) == 0


def test_export_games_filters_and_sorts():
    state = {
        "games": {
            "a": {"name": "A", "series": [80, 90, 100], "chart_at": "x", "score": 70, "sightings": 2},
            "b": {"name": "B", "series": [1, 2, 3], "chart_at": "x", "score": 5, "sightings": 1},
            "c": {"name": "C", "series": [], "chart_at": None, "score": 0, "sightings": 1},
        }
    }
    out = export_games(state, "now", min_peak=10)
    names = [i["name"] for i in out["items"]]
    assert names[0] == "A"      # high peak, passed min_peak
    assert "B" not in names     # peak < min_peak
    assert "C" in names         # pending (uncharted) still shown


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        return _Resp(self.payload)


def test_steam_new_parses_new_and_coming():
    payload = {
        "new_releases": {"items": [{"name": "Witch Storm", "id": 1, "url": "u1"}]},
        "coming_soon": {"items": [{"name": "Two Minds", "id": 2}]},
    }
    out = steam_new(_Session(payload))
    assert {c["name"] for c in out} == {"Witch Storm", "Two Minds"}
    assert out[0]["source"] == "steam_new"


def test_steam_new_assigns_rank_and_size():
    payload = {"new_releases": {"items": [{"name": "A", "id": 1}, {"name": "B", "id": 2}, {"name": "C", "id": 3}]}}
    out = steam_new(_Session(payload))
    assert [(c["name"], c["rank"], c["size"]) for c in out] == [("A", 1, 3), ("B", 2, 3), ("C", 3, 3)]


def test_appstore_new_uses_im_name():
    payload = {"feed": {"entry": [{"im:name": {"label": "Booking In Cloud"}, "id": {"label": "u"}}]}}
    out = appstore_new(_Session(payload), countries=["us"])
    assert out[0]["name"] == "Booking In Cloud"
    assert out[0]["source"] == "appstore:us"


def test_appstore_new_assigns_rank():
    payload = {"feed": {"entry": [
        {"im:name": {"label": "X"}, "id": {"label": "u1"}},
        {"im:name": {"label": "Y"}, "id": {"label": "u2"}},
    ]}}
    out = appstore_new(_Session(payload), countries=["us"])
    assert [(c["rank"], c["size"]) for c in out] == [(1, 2), (2, 2)]


def test_rank_change_signs_and_normalises():
    from collector.games.radar import rank_change

    assert rank_change(50, 100, 10, 100) == 0.4    # climbed toward the top
    assert rank_change(10, 100, 50, 100) == -0.4   # dropped
    assert rank_change(None, None, 10, 100) == 0.0
    assert rank_change(5, 0, 1, 10) == 0.0


def test_heat_includes_rank_momentum_and_position():
    from collector.games.radar import _heat, _rank_pos

    assert _heat({}) == 0
    assert _heat({"rankChange": 0.2}) == 200.0
    assert _heat({"growth": 800, "rankChange": 0.2}) == 800  # growth dominates
    assert _rank_pos({"rank": 1, "size": 10}) == 1.0
    assert _rank_pos({"rank": 10, "size": 10}) == 0.1
    assert _rank_pos({}) == 0.0


def test_chart_plan_specials_then_breakouts():
    from collector.games.radar import _chart_plan

    merged = {
        "web": {"name": "foo.app", "sources": ["trending:US"], "growth": 0, "rankChange": 0.0},
        "boom": {"name": "Boom Game", "sources": ["trending:US"], "growth": 900, "rankChange": 0.0},
        "climb": {"name": "Climber", "sources": ["appstore:us"], "growth": 0, "rankChange": 0.3,
                  "rank": 10, "size": 100},
        "meh": {"name": "Meh", "sources": ["appstore:us"], "growth": 0, "rankChange": 0.0},
    }
    plan = _chart_plan(merged, {}, max_chart=2, now="t")
    assert set(plan) == {"web", "boom"}                  # special, then breakout
    plan3 = _chart_plan(merged, {}, max_chart=3, now="t")
    assert set(plan3) == {"web", "boom", "climb"}        # then the climb


def test_chart_plan_top5_climbs_then_fills_rest():
    from collector.games.radar import _chart_plan

    merged = {
        f"c{i}": {"name": f"C{i}", "sources": ["appstore:us"], "growth": 0,
                  "rankChange": 0.1 + i * 0.01, "rank": i + 1, "size": 100}
        for i in range(8)
    }
    plan = _chart_plan(merged, {}, max_chart=7, now="t")
    assert len(plan) == 7
    assert {"c7", "c6", "c5", "c4", "c3"}.issubset(plan)   # the 5 biggest climbs


def test_chart_plan_skips_already_charted():
    from collector.games.radar import _chart_plan

    merged = {"a": {"name": "A", "sources": ["trending:US"], "growth": 900}}
    games = {"a": {"name": "A", "chart_at": "x", "series": [1, 2]}}
    assert _chart_plan(merged, games, max_chart=5, now="t") == []


def test_skip_low_value_gates():
    from collector.games.radar import _skip_low_value

    # known Google growth below 200% -> skip; exactly 200% -> keep
    assert _skip_low_value({"hasGrowth": True, "growth": 150}) is True
    assert _skip_low_value({"hasGrowth": True, "growth": 200}) is False
    assert _skip_low_value({"growth": 50}) is False          # no growth signal -> not gated
    # ranked in the bottom 30% -> skip
    assert _skip_low_value({"rank": 80, "size": 100}) is True     # position 0.21
    assert _skip_low_value({"rank": 70, "size": 100}) is False    # position 0.31
    assert _skip_low_value({"name": "x.app"}) is False            # no rank -> not gated


def test_skip_low_value_respects_threshold_overrides():
    from collector.games.radar import _skip_low_value

    m = {"hasGrowth": True, "growth": 120}
    assert _skip_low_value(m) is True
    assert _skip_low_value(m, min_growth=100) is False
    m2 = {"rank": 80, "size": 100}   # position 0.21
    assert _skip_low_value(m2) is True
    assert _skip_low_value(m2, min_pos=0.10) is False


def test_is_hot_growth_or_rank_surge():
    from collector.games.radar import _is_hot

    assert _is_hot({"hasGrowth": True, "growth": 500}) is True
    assert _is_hot({"hasGrowth": True, "growth": 499}) is False
    assert _is_hot({"growth": 900}) is False            # no fresh-growth flag
    assert _is_hot({"rankChange": 0.25}) is True
    assert _is_hot({"rankChange": 0.10}) is False
    assert _is_hot({}) is False


def test_chart_plan_drops_weak_and_buried():
    from collector.games.radar import _chart_plan

    merged = {
        "weak": {"name": "Weak", "sources": ["trending:US"], "growth": 120, "hasGrowth": True},
        "buried": {"name": "Buried", "sources": ["appstore:us"], "growth": 0, "rank": 95, "size": 100},
        "good": {"name": "Good", "sources": ["appstore:us"], "growth": 0, "rank": 5, "size": 100},
    }
    assert set(_chart_plan(merged, {}, max_chart=5, now="t")) == {"good"}


def test_rec_candidate_from_pending_backlog():
    from collector.games.radar import _chart_plan, _rec_candidate

    # A game that fell off every source list (not in `merged`), kept in state.
    rec = {"name": "gta leaker", "sources": ["trending:US"], "growth": 1000,
           "first": "t0", "chart_at": None}
    pool = {"gta leaker": _rec_candidate(rec)}
    plan = _chart_plan(pool, {"gta leaker": rec}, max_chart=3, now="t1")
    assert plan == ["gta leaker"]           # backlog breakout still gets a curve


def test_from_trending_games_filters_category_6_and_noise():
    trends = {"items": {"US": [
        {"q": "some game", "cats": [6]},
        {"q": "sports game", "cats": [17]},
        {"q": "powerball", "cats": [6], "noise": "彩票"},
    ]}}
    out = from_trending_games(trends)
    assert [c["name"] for c in out] == ["some game"]


def test_from_trending_games_keeps_webgames_and_growth_drops_junk():
    trends = {"items": {"US": [
        {"q": "wordle hints", "cats": [6], "growth": 1000},
        {"q": "aion 2 server status", "cats": [6], "growth": 900},
        {"q": "lagoslife.app", "cats": [], "growth": 500, "zh": "拉各斯生活"},
    ]}}
    out = from_trending_games(trends)
    assert [c["name"] for c in out] == ["lagoslife.app"]
    assert out[0]["growth"] == 500
    assert out[0]["zh"] == "拉各斯生活"


def test_is_web_game():
    assert is_web_game("lagoslife.app")
    assert is_web_game("skribbl.io")
    assert not is_web_game("GTA 6")


def test_export_games_ranks_new_breakout_first():
    state = {"games": {
        "old": {"name": "Old Game", "series": [50, 60, 70], "chart_at": "x", "score": 90,
                "first": "t0", "last": "t1", "growth": 0},
        "hot": {"name": "NewHot", "series": [], "chart_at": None, "score": 0,
                "first": "now", "last": "now", "growth": 1000},
    }}
    out = export_games(state, "now")
    assert out["items"][0]["name"] == "NewHot"
    assert out["items"][0]["new"] is True
    assert out["items"][0]["breakout"] is True
    assert out["items"][1]["name"] == "Old Game"


def test_export_games_marks_web_game():
    state = {"games": {
        "w": {"name": "foo.app", "series": [], "chart_at": None, "first": "now", "last": "now"},
    }}
    out = export_games(state, "now")
    assert out["items"][0]["web_game"] is True


def test_rise_ratio_detects_sudden_spike():
    assert rise_ratio([50] * 30) < 0.2          # flat -> ~0
    assert rise_ratio([0] * 20 + [100] * 10) > 1.0   # quiet then spike -> high
    assert rise_ratio([]) == 0.0


def test_export_games_rise_marks_breakout_and_ranks_first():
    state = {"games": {
        "quiet": {"name": "Quiet Game", "series": [0, 0, 0, 0, 0, 0, 90, 100, 120],
                  "chart_at": "x", "score": 40, "first": "t0", "last": "t1", "rise": 5.0},
        "steady": {"name": "Steady", "series": [50, 50, 50],
                   "chart_at": "x", "score": 80, "first": "t0", "last": "t1", "rise": 0.0},
    }}
    out = export_games(state, "now", rise_breakout=1.0)
    assert out["items"][0]["name"] == "Quiet Game"
    assert out["items"][0]["breakout"] is True
    assert out["items"][1]["breakout"] is False


def test_is_due_for_refresh():
    from datetime import datetime, timedelta, timezone

    from collector.games.radar import _is_due

    now = datetime.now(timezone.utc).isoformat()
    assert _is_due(None, now, 12) is True
    assert _is_due((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), now, 12) is False
    assert _is_due((datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(), now, 12) is True


def test_heat_takes_larger_of_rise_and_growth():
    from collector.games.radar import _heat

    assert _heat({"rise": 3.0}) == 300          # 环比 3.0 -> 300%
    assert _heat({"growth": 800}) == 800
    assert _heat({"rise": 3.0, "growth": 800}) == 800
    assert _heat({}) == 0
