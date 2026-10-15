from collector.enrich import rule_noise


def test_sports_leagues_and_clubs_are_noise():
    assert rule_noise("premier league", []) == "体育"
    assert rule_noise("la liga", []) == "体育"
    assert rule_noise("al nassr", []) == "体育"
    assert rule_noise("tottenham", []) == "体育"


def test_sports_events_are_fixtures():
    assert rule_noise("nba fixtures", []) == "比赛"
    assert rule_noise("nba finals highlights", []) == "比赛"
    assert rule_noise("team a vs team b", []) == "比赛"


def test_common_person_names_are_noise():
    assert rule_noise("john smith", []) == "人物"
    assert rule_noise("david miller", []) == "人物"
    assert rule_noise("sarah johnson", []) == "人物"


def test_brands_are_noise():
    assert rule_noise("ticketmaster", []) == "品牌"
    assert rule_noise("hbo max", []) == "品牌"
    assert rule_noise("cnn live", []) == "品牌"


def test_result_still_lottery_not_fixture():
    assert rule_noise("powerball result", []) == "彩票"


def test_other_existing_rules_still_work():
    assert rule_noise("hurricane watch", []) == "天气"
    assert rule_noise("car crash", []) == "事故"


def test_clean_evergreen_terms_are_kept():
    assert rule_noise("ai website builder", []) == ""
    assert rule_noise("best mutual funds", []) == ""
    assert rule_noise("sleep gummies", []) == ""
    assert rule_noise("openai", []) == ""      # niche-adjacent brand stays clean
    assert rule_noise("crypto", []) == ""
