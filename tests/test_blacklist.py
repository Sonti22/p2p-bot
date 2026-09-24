import blacklist


def test_add_list_and_blocked(tmp_path):
    db = str(tmp_path / "blacklist.db")
    blacklist.add("Bybit", "Плохой Мерчант", path=db)
    blacklist.add("BestChange", "Обменник [TRC20]", path=db)
    rows = blacklist.list_all(path=db)
    assert [(ex, nick) for _, ex, nick in rows] == [("BestChange", "Обменник [TRC20]"), ("Bybit", "Плохой Мерчант")]
    assert blacklist.blocked(path=db) == {("Bybit", "Плохой Мерчант"), ("BestChange", "Обменник [TRC20]")}


def test_add_duplicate_ignored(tmp_path):
    db = str(tmp_path / "blacklist.db")
    blacklist.add("Bybit", "nick", path=db)
    blacklist.add("Bybit", "nick", path=db)
    assert len(blacklist.list_all(path=db)) == 1


def test_remove(tmp_path):
    db = str(tmp_path / "blacklist.db")
    blacklist.add("Bybit", "nick", path=db)
    entry_id = blacklist.list_all(path=db)[0][0]
    blacklist.remove(entry_id, path=db)
    assert blacklist.list_all(path=db) == []


def test_list_and_blocked_empty_when_db_missing(tmp_path):
    db = str(tmp_path / "none.db")
    assert blacklist.list_all(path=db) == []
    assert blacklist.blocked(path=db) == set()
