"""Торговое ядро, gates: пороги плана «бумага → кнопка → автомат»; статистика бэктеста — только из локального файла
владельца в data/ (не из git, с sha кода research/); укороченная бумага — только с флагом владельца в .env; minlot сразу
после сильного бэктеста, автомат — только при пройденных порогах; математика."""
import hashlib
import json
import math
import os
import time

import pytest

from trading import gates, switch

HEDGE_PAPER = {"days": 14, "count": 50, "ratio_ok_share": 0.95, "cost_to_buffer": 0.6, "sigma_ratio": 0.5}
HEDGE_LIVE = {"days": 21, "count": 30, "model_divergence_pp": 0.05, "unknown_over_10min": 0, "limit_violations": 0}
HEDGE_BT = {"days": 14, "count": 50, "ratio_ok_share": 0.95, "cost_to_buffer": 0.6, "sigma_ratio": 0.5}
FUND_PAPER = {"days": 60, "payments": 90, "apr_over_earn_pp": 3, "max_drawdown": 0.02}
FUND_LIVE = {"days": 30, "cycles": 20, "within_paper_1sigma": True, "unknown_over_10min": 0, "limit_violations": 0}
FUND_BT = {"days": 60, "payments": 90, "apr_over_earn_pp": 3, "max_drawdown": 0.02}
DIR_BT = {"months_oos": 12, "trades": 200, "sharpe": 1.0, "profit_factor": 1.2, "p_value_vs_random": 0.049}
DIR_PAPER = {"days": 90, "trades": 50, "in_backtest_ci90": True}
DIR_LIVE = {"days": 60, "trades": 30, "max_drawdown": 0.099, "unknown_over_10min": 0, "limit_violations": 0}
MAKER_PAPER = {"days": 30, "net_after_fee": 0.01}
MAKER_LIVE = {"confirmed_edits": 100, "corridor_exits": 0, "unknown_over_10min": 0, "limit_violations": 0}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(gates, "_FLAGS", {"short_paper": False})


def _varint(value):
    """git encode_varint (индекс v4)."""
    out = [value & 127]
    value >>= 7
    while value:
        value -= 1
        out.insert(0, 128 | (value & 127))
        value >>= 7
    return bytes(out)


def git_index(paths, version=2, sparse=(), link=None):
    """Индекс git по формату index-format.txt (как пишет git): записи 62 байта + путь (v2/v3 — с выравниванием до 8,
    v4 — сжатие префикса), расширение «link» (split index: 20 байт id общего индекса), SHA-1 содержимого в конце.
    sparse — папки-записи (mode 040000, sparse index)."""
    entries = sorted([(p, 0o100644) for p in paths] + [(d.rstrip("/") + "/", 0o040000) for d in sparse])
    out, prev = bytearray(b"DIRC" + version.to_bytes(4, "big") + len(entries).to_bytes(4, "big")), b""
    for path, mode in entries:
        name = path.encode()
        head = bytes(24) + mode.to_bytes(4, "big") + bytes(12) + hashlib.sha1(name).digest() + \
            min(len(name), 0xFFF).to_bytes(2, "big")
        if version == 4:
            common = 0
            while common < min(len(prev), len(name)) and prev[common] == name[common]:
                common += 1
            out += head + _varint(len(prev) - common) + name[common:] + b"\0"
        else:
            size = ((62 + len(name) + 8) // 8) * 8
            out += head + name + b"\0" * (size - 62 - len(name))
        prev = name
    if link is not None:                                   # id общего индекса + пустые битовые карты delete/replace
        body = link + bytes(24)
        out += b"link" + len(body).to_bytes(4, "big") + body
    return bytes(out) + hashlib.sha1(bytes(out)).digest()


def bot_root(tmp_path, stats=None, *, sha=None, tracked=False, version=1, ts=None, worktree=False, index_version=2):
    """Папка «бота»: research/ с кодом, data/gates_backtest.json, индекс git (.git — папка или файл worktree)."""
    root = tmp_path / "bot"
    (root / "research").mkdir(parents=True, exist_ok=True)
    (root / "research" / "hedge_bt.py").write_bytes(b"X = 1\n")
    (root / "data").mkdir(exist_ok=True)
    paths = ["research/hedge_bt.py", "research/report.py"] + (["data/gates_backtest.json"] if tracked else [])
    index = git_index(paths, index_version)
    if worktree:
        gitdir = tmp_path / "gitdir"
        gitdir.mkdir(exist_ok=True)
        (gitdir / "index").write_bytes(index)
        (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    else:
        (root / ".git").mkdir(exist_ok=True)
        (root / ".git" / "index").write_bytes(index)
    data = {"version": version, "generated_at": time.time() - 60 if ts is None else ts,
            "research_sha": sha or gates.research_sha(str(root)),
            "strategies": stats if stats is not None else {"hedge": HEDGE_BT, "funding": FUND_BT,
                                                           "directional": DIR_BT}}
    (root / "data" / "gates_backtest.json").write_text(json.dumps(data), encoding="utf-8")
    return str(root)


def loaded(tmp_path, strategy, stats=None):
    root = bot_root(tmp_path, stats)
    bt, why = gates.load_backtest(strategy, root=root)
    assert bt is not None, why
    return bt


def test_modes_match_switch():
    assert gates.MODES == switch.MODES


def test_thresholds_equal_the_plan():
    """Пороги «сильного бэктеста» — ровно из плана (TODO владельца сняты): хедж/фандинг — пороги бумаги плана,
    направленная — бэктест плана; укороченная бумага меняет только срок."""
    assert gates.PAPER_TO_BUTTON["hedge"] == [("days", ">=", 14, "дней бумаги"), ("count", ">=", 50, "хеджей"),
                                              ("ratio_ok_share", ">=", 0.95, "доля хеджей с коэффициентом 0.9–1.1"),
                                              ("cost_to_buffer", "<=", 0.6, "стоимость хеджа / запас на курс"),
                                              ("sigma_ratio", "<=", 0.5, "σ(факт − план) с хеджем / без хеджа")]
    assert [r[:3] for r in gates.PAPER_TO_BUTTON["funding"]] == [("days", ">=", 60), ("payments", ">=", 90),
                                                                 ("apr_over_earn_pp", ">=", 3),
                                                                 ("max_drawdown", "<=", 0.02)]
    assert gates.BACKTEST_STRONG["hedge"] == gates.PAPER_TO_BUTTON["hedge"]
    assert gates.BACKTEST_STRONG["funding"] == gates.PAPER_TO_BUTTON["funding"]
    assert [r[:3] for r in gates.BACKTEST_STRONG["directional"]] == [
        ("months_oos", ">=", 12), ("trades", ">=", 200), ("sharpe", ">=", 1), ("profit_factor", ">=", 1.2),
        ("p_value_vs_random", "<", 0.05)]
    assert gates.SHORT_PAPER == {"hedge": {"days": 14}, "funding": {"days": 14}, "directional": {"days": 30}}
    src = open(gates.__file__, encoding="utf-8").read()
    assert "TODO(владелец)" not in src and "TODO(owner)" not in src


@pytest.mark.parametrize("strategy,paper", [("hedge", HEDGE_PAPER), ("funding", FUND_PAPER), ("maker", MAKER_PAPER)])
def test_paper_to_button_exact_thresholds_pass(strategy, paper):
    assert gates.paper_to_button(strategy, paper).passed


@pytest.mark.parametrize("strategy,paper,key,value", [
    ("hedge", HEDGE_PAPER, "days", 13.9), ("hedge", HEDGE_PAPER, "count", 49),
    ("hedge", HEDGE_PAPER, "ratio_ok_share", 0.9499), ("hedge", HEDGE_PAPER, "cost_to_buffer", 0.61),
    ("hedge", HEDGE_PAPER, "sigma_ratio", 0.51), ("funding", FUND_PAPER, "days", 59),
    ("funding", FUND_PAPER, "payments", 89), ("funding", FUND_PAPER, "apr_over_earn_pp", 2.99),
    ("funding", FUND_PAPER, "max_drawdown", 0.021), ("maker", MAKER_PAPER, "net_after_fee", 0),
    ("maker", MAKER_PAPER, "days", 29),
    ("hedge", HEDGE_PAPER, "count", None), ("hedge", HEDGE_PAPER, "count", float("nan")),
    ("hedge", HEDGE_PAPER, "count", "много"), ("hedge", HEDGE_PAPER, "count", True),
])
def test_paper_to_button_each_threshold_fails(strategy, paper, key, value):
    g = gates.paper_to_button(strategy, dict(paper, **{key: value}))
    assert not g.passed and any(key in f or "нужно" in f for f in g.failures)


@pytest.mark.parametrize("strategy,paper", [("hedge", HEDGE_PAPER), ("funding", FUND_PAPER)])
def test_missing_stat_fails_closed(strategy, paper):
    for key in paper:
        g = gates.paper_to_button(strategy, {k: v for k, v in paper.items() if k != key})
        assert not g.passed and any("нет данных" in f and key in f for f in g.failures)
    assert not gates.paper_to_button(strategy, None).passed and not gates.paper_to_button(strategy, []).passed


# --- статистика бэктеста — только из локального файла владельца ---

def test_backtest_dict_never_counts(tmp_path):
    """Словарь (в т. ч. из файла в репозитории, прочитанного кем-то ещё) — не бэктест: только load_backtest."""
    for strategy, stats in (("hedge", HEDGE_BT), ("funding", FUND_BT), ("directional", DIR_BT)):
        g = gates.backtest_strong(strategy, stats)
        assert not g.passed and "только из локального файла" in g.failures[0]
        assert gates.max_mode(strategy, backtest=stats) == "paper"
    fake = gates.Backtest("hedge", HEDGE_BT, "x", 0, "p", object())                    # не из загрузчика
    assert not gates.backtest_strong("hedge", fake).passed
    bt = loaded(tmp_path, "hedge")
    assert gates.backtest_strong("hedge", bt).passed and not gates.backtest_strong("funding", bt).passed


@pytest.mark.parametrize("case,why", [
    ("tracked", "есть в git"), ("sha", "sha не совпал"), ("version", "версия"), ("future", "время создания"),
    ("no_strategy", "нет статистики стратегии"), ("broken_json", "битый JSON"), ("missing", "нет локального файла"),
    ("outside_data", "только из локального файла в data/"), ("no_git", "нет индекса git"),
])
def test_load_backtest_refusals(tmp_path, case, why):
    kw = {"tracked": dict(tracked=True), "sha": dict(sha="0" * 64), "version": dict(version=2),
          "future": dict(ts=time.time() + 3600), "no_strategy": dict(stats={"funding": FUND_BT})}.get(case, {})
    root = bot_root(tmp_path, **kw)
    path = None
    if case == "broken_json":
        open(os.path.join(root, "data", "gates_backtest.json"), "w", encoding="utf-8").write("{")
    elif case == "missing":
        os.remove(os.path.join(root, "data", "gates_backtest.json"))
    elif case == "outside_data":
        path = os.path.join(root, "research", "gates_backtest.json")                    # файл из репозитория
        open(path, "w", encoding="utf-8").write(open(os.path.join(root, "data", "gates_backtest.json")).read())
    elif case == "no_git":
        os.remove(os.path.join(root, ".git", "index"))
    bt, reason = gates.load_backtest("hedge", path=path, root=root)
    assert bt is None and why in reason, reason


@pytest.mark.parametrize("index_version", [2, 3, 4])
def test_tracked_stats_file_refused_for_every_index_version(tmp_path, index_version):
    """Индекс разбирается по формату git: в v4 пути сжаты префиксом («data/gates_backtest.json» целиком в байтах
    индекса нет) — закоммиченная статистика всё равно не проходит; незакоммиченная — проходит."""
    root = bot_root(tmp_path, tracked=True, index_version=index_version)
    index = open(os.path.join(root, ".git", "index"), "rb").read()
    if index_version == 4:
        assert b"data/gates_backtest.json" in index or b"gates_backtest.json" in index
    bt, why = gates.load_backtest("hedge", root=root)
    assert bt is None and "есть в git" in why, why
    clean = bot_root(tmp_path / "clean", index_version=index_version)
    assert gates.load_backtest("hedge", root=clean)[0] is not None


def test_committed_stats_file_found_with_v4_prefix_compression():
    """Раньше: подстрока пути в байтах индекса; в v4 путь «data/gates_backtest.json» после «data/a.json» хранится как
    «gates_backtest.json» — подстрока не находилась."""
    data = git_index(["data/a.json", "data/gates_backtest.json", "research/hedge_bt.py"], 4)
    assert b"data/gates_backtest.json" not in data
    paths, dirs = gates.index_entries(data)
    assert paths == {"data/a.json", "data/gates_backtest.json", "research/hedge_bt.py"} and dirs == set()
    sparse = git_index(["research/hedge_bt.py"], 2, sparse=["data"])
    assert gates.index_entries(sparse) == ({"research/hedge_bt.py"}, {"data"})


@pytest.mark.parametrize("damage", ["version", "checksum", "truncated", "garbage"])
def test_unparsable_index_refuses(tmp_path, damage):
    root = bot_root(tmp_path)
    path = os.path.join(root, ".git", "index")
    data = open(path, "rb").read()
    if damage == "version":
        body = data[:4] + (5).to_bytes(4, "big") + data[8:-20]
        data = body + hashlib.sha1(body).digest()
    elif damage == "checksum":
        data = data[:-1] + bytes([data[-1] ^ 1])
    elif damage == "truncated":
        body = data[:40]
        data = body + hashlib.sha1(body).digest()
    else:
        data = b"DIRC\x00\x00\x00\x02research/hedge_bt.py\x00"          # как раньше подделывал тест: не индекс
    open(path, "wb").write(data)
    bt, why = gates.load_backtest("hedge", root=root)
    assert bt is None and "не разобран" in why, why


@pytest.mark.parametrize("tracked", [True, False])
def test_split_index_shared_entries_count(tmp_path, tracked):
    """Split index (git update-index --split-index): записи — в sharedindex.<id>, в самом индексе — только расширение
    «link». Статистика в общем индексе — «есть в git»; нет общего индекса — не разобрать (порог не пройден)."""
    root = bot_root(tmp_path)
    shared = git_index(["research/hedge_bt.py"] + (["data/gates_backtest.json"] if tracked else []))
    sid = hashlib.sha1(shared).digest()
    with open(os.path.join(root, ".git", f"sharedindex.{sid.hex()}"), "wb") as f:
        f.write(shared)
    with open(os.path.join(root, ".git", "index"), "wb") as f:
        f.write(git_index([], link=sid))
    bt, why = gates.load_backtest("hedge", root=root)
    assert (bt is None and "есть в git" in why) if tracked else bt is not None, why
    os.remove(os.path.join(root, ".git", f"sharedindex.{sid.hex()}"))
    bt, why = gates.load_backtest("hedge", root=root)
    assert bt is None and "не разобран" in why


def test_stats_binding_is_not_called_a_signature():
    """sha кода research/ — привязка, не подпись: так и написано (кто пишет в data/, впишет любые цифры)."""
    src = open(gates.__file__, encoding="utf-8").read()
    assert "НЕ подпись" in src and "подписана" not in src


def test_load_backtest_worktree_git_file_and_sha_follows_code(tmp_path):
    root = bot_root(tmp_path, worktree=True)
    bt, why = gates.load_backtest("hedge", root=root)
    assert bt is not None and bt.research_sha == gates.research_sha(root), why
    code = os.path.join(root, "research", "hedge_bt.py")
    with open(code, "ab") as f:
        f.write(b"Y = 2\n")                                                            # код research/ изменился
    assert gates.load_backtest("hedge", root=root)[0] is None
    lf = gates.research_sha(root)
    with open(code, "wb") as f:
        f.write(b"X = 1\r\nY = 2\r\n")
    assert gates.research_sha(root) == lf                                              # перевод строк не важен


def test_directional_needs_strong_backtest_and_ci(tmp_path):
    bt = loaded(tmp_path, "directional")
    assert not gates.paper_to_button("directional", DIR_PAPER).passed               # без бэктеста — нет
    assert not gates.paper_to_button("directional", DIR_PAPER, DIR_BT).passed       # словарь — не бэктест
    assert gates.paper_to_button("directional", DIR_PAPER, bt).passed is True
    for key, value in (("months_oos", 11), ("trades", 199), ("sharpe", 0.99), ("profit_factor", 1.19),
                       ("p_value_vs_random", 0.05)):
        weak = gates.load_backtest("directional", root=bot_root(tmp_path, {"directional": dict(DIR_BT,
                                                                                               **{key: value})}))[0]
        g = gates.paper_to_button("directional", DIR_PAPER, weak)
        assert not g.passed and "бэктест не прошёл" in g.failures[0]
    assert not gates.paper_to_button("directional", dict(DIR_PAPER, in_backtest_ci90=False), bt).passed
    assert not gates.paper_to_button("directional", dict(DIR_PAPER, in_backtest_ci90=1), bt).passed


def test_short_paper_only_with_owner_flag(tmp_path):
    """Сильный бэктест укорачивает бумагу (хедж/фандинг 14 дней, направленная 30) только при флаге владельца
    TRADING_SHORT_PAPER=1 в .env; минимумы количества — как в плане."""
    fund = loaded(tmp_path, "funding")
    short_f = {"days": 14, "payments": 90, "apr_over_earn_pp": 3, "max_drawdown": 0.02}
    assert not gates.paper_to_button("funding", short_f, fund).passed               # без флага — 60 дней
    env = tmp_path / ".env"
    env.write_text("TRADING_SHORT_PAPER=1\n", encoding="utf-8")
    assert gates.flags_from_file(str(env)) == {"short_paper": True}
    assert gates.paper_to_button("funding", short_f, fund).passed
    assert not gates.paper_to_button("funding", dict(short_f, payments=42), fund).passed   # выплат — как в плане
    assert not gates.paper_to_button("funding", dict(short_f, days=13), fund).passed
    assert not gates.paper_to_button("funding", short_f).passed                      # без бэктеста — нет
    assert not gates.paper_to_button("funding", short_f, FUND_BT).passed             # словарь — не бэктест
    dir_bt = gates.load_backtest("directional", root=bot_root(tmp_path))[0]
    short_d = {"days": 30, "trades": 50, "in_backtest_ci90": True}
    assert gates.paper_to_button("directional", short_d, dir_bt).passed
    assert not gates.paper_to_button("directional", dict(short_d, days=29), dir_bt).passed
    assert not gates.paper_to_button("directional", dict(short_d, trades=49), dir_bt).passed
    assert not gates.backtest_strong("maker", MAKER_PAPER).passed                # мейкеру бэктест бумагу не заменяет


@pytest.mark.parametrize("text,on", [("TRADING_SHORT_PAPER=1\n", True), ("trading_short_paper=1 # владелец\n", True),
                                     ("TRADING_SHORT_PAPER=0\n", False), ("TRADING_SHORT_PAPER=true\n", False),
                                     ("TRADING_SHORT_PAPER=1\nTRADING_SHORT_PAPER=0\n", False), ("", False),
                                     ("# TRADING_SHORT_PAPER=1\n", False)])
def test_short_paper_flag_from_env_file_only(tmp_path, monkeypatch, text, on):
    monkeypatch.setenv("TRADING_SHORT_PAPER", "1")                              # окружение процесса не считается
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    assert gates.flags_from_file(str(env))["short_paper"] is on and gates.short_paper_enabled() is on
    assert gates.flags_from_file(str(tmp_path / "нет.env"))["short_paper"] is False


@pytest.mark.parametrize("strategy,live,key,value", [
    ("hedge", HEDGE_LIVE, "days", 20), ("hedge", HEDGE_LIVE, "count", 29),
    ("hedge", HEDGE_LIVE, "model_divergence_pp", 0.051), ("hedge", HEDGE_LIVE, "unknown_over_10min", 1),
    ("hedge", HEDGE_LIVE, "limit_violations", 1), ("funding", FUND_LIVE, "cycles", 19),
    ("funding", FUND_LIVE, "within_paper_1sigma", False), ("directional", DIR_LIVE, "max_drawdown", 0.10),
    ("directional", DIR_LIVE, "trades", 29), ("maker", MAKER_LIVE, "corridor_exits", 1),
    ("maker", MAKER_LIVE, "confirmed_edits", 99), ("funding", FUND_LIVE, "unknown_over_10min", 2),
])
def test_button_to_auto_thresholds(strategy, live, key, value):
    assert gates.button_to_auto(strategy, live).passed
    assert not gates.button_to_auto(strategy, dict(live, **{key: value})).passed


def test_auto_common_required_for_every_strategy():
    for strategy, live in (("hedge", HEDGE_LIVE), ("funding", FUND_LIVE), ("directional", DIR_LIVE),
                           ("maker", MAKER_LIVE)):
        g = gates.button_to_auto(strategy, {k: v for k, v in live.items() if k != "limit_violations"})
        assert not g.passed and any("limit_violations" in f for f in g.failures)


def test_max_mode_ladder_and_allow(tmp_path):
    hedge_bt, dir_bt = loaded(tmp_path, "hedge"), loaded(tmp_path, "directional")
    assert gates.max_mode("hedge") == "paper"
    assert gates.max_mode("hedge", backtest=hedge_bt) == "minlot"                # сразу после сильного бэктеста
    assert gates.max_mode("hedge", backtest=HEDGE_BT) == "paper"                 # словарь — нет
    assert gates.max_mode("hedge", HEDGE_PAPER) == "confirm"
    assert gates.max_mode("hedge", HEDGE_PAPER, live=HEDGE_LIVE) == "auto"
    assert gates.max_mode("hedge", backtest=hedge_bt, live=HEDGE_LIVE) == "minlot"   # автомат — только после бумаги
    assert gates.max_mode("directional", DIR_PAPER) == "paper"
    assert gates.max_mode("directional", DIR_PAPER, dir_bt, DIR_LIVE) == "auto"
    assert gates.max_mode("maker", MAKER_PAPER, backtest={"anything": 1}) == "confirm"
    g = gates.allow("hedge", "auto", HEDGE_PAPER, live=dict(HEDGE_LIVE, unknown_over_10min=1))
    assert not g.passed and "только confirm" in g.failures[0] and any("unknown" in f for f in g.failures)
    assert gates.allow("hedge", "minlot", backtest=hedge_bt).passed
    assert not gates.allow("hedge", "minlot").passed and "нет бэктеста" in gates.allow("hedge", "minlot").failures
    assert not gates.allow("hedge", "minlot", backtest=HEDGE_BT).passed
    assert gates.allow("hedge", "paper").passed
    assert not gates.allow("hedge", "turbo").passed
    assert gates.allow("unknown", "paper").passed                                  # бумага разрешена всегда
    assert not gates.allow("arb", "minlot", backtest=hedge_bt).passed


def test_effective_mode_with_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "auto")
    assert switch.effective_mode(gates.max_mode("hedge", backtest=loaded(tmp_path, "hedge"))) == "minlot"
    monkeypatch.setenv("TRADING_MODE", "minlot")
    assert switch.effective_mode(gates.max_mode("hedge", HEDGE_PAPER, live=HEDGE_LIVE)) == "minlot"


# --- математика ---

def test_math_helpers():
    assert gates.mean([1, 2, 3]) == 2 and gates.mean([]) is None
    assert gates.stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.138089935299395)
    assert gates.stdev([1]) is None
    assert gates.sharpe([0.01, 0.02, 0.03], 252) == pytest.approx(0.02 / 0.01 * math.sqrt(252))
    assert gates.sharpe([0.01, 0.01], 252) is None
    assert gates.profit_factor([10, -5, 5, -5]) == 1.5
    assert gates.profit_factor([1, 2]) == math.inf and gates.profit_factor([]) is None
    assert gates.profit_factor([-1]) == 0
    assert gates.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert gates.max_drawdown([100, 101]) == 0
    with pytest.raises(ValueError):
        gates.max_drawdown([100, 0])
    lo, hi = gates.ci90(0.5, 2.0, 16)
    assert (lo, hi) == pytest.approx((0.5 - 1.6448536 * 0.5, 0.5 + 1.6448536 * 0.5))
    assert gates.within_ci90(1.3, 0.5, 2.0, 16) and not gates.within_ci90(1.33, 0.5, 2.0, 16)
    assert not gates.within_ci90(0.5, 0.5, None, 16) and not gates.within_ci90(0.5, 0.5, 1, 0)
    assert gates.within_sigma(1.09, 1.0, 0.1) and gates.within_sigma(0.91, 1.0, 0.1)
    assert not gates.within_sigma(1.11, 1.0, 0.1)
    assert not gates.within_sigma(1.0, 1.0, None)
