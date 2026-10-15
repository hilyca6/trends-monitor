import json

import pytest

from collector.quota import Quota, QuotaExceeded


def test_reserve_increments_and_persists(tmp_path):
    q = Quota(tmp_path / "u.json", monthly_limit=3, daily_limit=2)
    q.reserve()
    u = q.reserve()
    assert u["monthUsed"] == 2 and u["dayUsed"] == 2
    assert q.available() is False
    assert q.remaining() == (1, 0)
    with pytest.raises(QuotaExceeded):
        q.reserve()
    assert (tmp_path / "u.json").exists()


def test_rollover_on_new_day(tmp_path):
    q = Quota(tmp_path / "u.json", monthly_limit=10, daily_limit=1)
    q.reserve()
    data = json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))
    data["day"] = "2000-01-01"  # pretend the last use was a past day
    (tmp_path / "u.json").write_text(json.dumps(data), encoding="utf-8")
    u = q.reserve()
    assert u["dayUsed"] == 1          # day counter rolled over
    assert u["monthUsed"] == 2        # month counter kept


def test_month_rollover_resets_both(tmp_path):
    q = Quota(tmp_path / "u.json", monthly_limit=10, daily_limit=10)
    q.reserve()
    data = json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))
    data["month"] = "2000-01"
    (tmp_path / "u.json").write_text(json.dumps(data), encoding="utf-8")
    u = q.reserve()
    assert u["monthUsed"] == 1 and u["dayUsed"] == 1


def test_reserve_many_raises_without_partial_write(tmp_path):
    q = Quota(tmp_path / "u.json", monthly_limit=5, daily_limit=5)
    with pytest.raises(QuotaExceeded):
        q.reserve(10)
    assert q.remaining() == (5, 5)  # nothing reserved on failure
