"""Пороги перехода «бумага → кнопка → автомат» — таблица «Пороги перехода» плана; чистые сравнения и загрузка
статистики бэктеста из локального файла владельца.

Режимы: paper < minlot < confirm < auto (trading.switch.MODES).
- minlot (кнопка, минимальный лот) — сразу после сильного бэктеста (решение владельца 2026-09-27, «нужно быстрее»);
- confirm (кнопка, полные потолки) — порог «бумага → кнопка» плана; укороченная бумага при сильном бэктесте (SHORT_PAPER:
  хедж/фандинг 14 дней, направленная 30 — решение владельца «7–14 дней, направленная 30») — ТОЛЬКО при явном флаге
  владельца TRADING_SHORT_PAPER=1 в .env на ПК (`flags_from_file` при старте; окружение процесса не считается);
  остальные условия качества и минимумы количества — как в плане;
- auto — confirm пройден И порог «кнопка → авто» по реальным сделкам И 0 unknown дольше 10 мин и 0 нарушений лимитов.
Статистика бэктеста — ТОЛЬКО из локального файла, который владелец сгенерировал у себя (`load_backtest`,
data/gates_backtest.json): путь внутри data/ бота (data/ — в .gitignore), файла нет в индексе git — индекс разбирается
по формату git (версии 2, 3 и 4 со сжатием путей, split index, sparse-папки), а не поиском подстроки; незнакомый
формат — порог не пройден (закоммиченная статистика порог не проходит никогда); в файле sha256 кода research/,
который её посчитал, и он совпадает с кодом research/ сейчас. Это привязка к коду, а НЕ подпись: кто может писать в
data/ на ПК владельца, может вписать туда любые цифры с текущим sha — защита от подделки здесь только в том, что data/
локальна и не приходит из git. Словарь или другой объект вместо загруженного файла — не бэктест: порог не пройден.
Нет нужной цифры, NaN или не число — порог не пройден (fail closed). Статистика бумаги и реальной торговли — словари от
кода бумаги/журнала; здесь только сравнения и простая математика (Sharpe, PF, просадка, 90% ДИ).
"""
import hashlib
import json
import math
import os
import time
from collections import namedtuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKTEST_FILE = "gates_backtest.json"    # в data/ бота
BACKTEST_VERSION = 1
RESEARCH_DIR = "research"
SHORT_PAPER_FLAG = "TRADING_SHORT_PAPER"
_FLAGS = {"short_paper": False}          # только из .env на ПК при старте (flags_from_file)

HEDGE, FUNDING, DIRECTIONAL, MAKER = "hedge", "funding", "directional", "maker"
STRATEGIES = (HEDGE, FUNDING, DIRECTIONAL, MAKER)
MODES = ("paper", "minlot", "confirm", "auto")
Z90 = 1.6448536269514722   # двусторонний 90%: Φ⁻¹(0.95)

Gate = namedtuple("Gate", "passed failures")

# (ключ, сравнение, порог, пояснение). Сравнения: ">=", "<=", ">", "<", "==".
PAPER_TO_BUTTON = {
    HEDGE: [("days", ">=", 14, "дней бумаги"), ("count", ">=", 50, "хеджей"),
            ("ratio_ok_share", ">=", 0.95, "доля хеджей с коэффициентом 0.9–1.1"),
            ("cost_to_buffer", "<=", 0.6, "стоимость хеджа / запас на курс"),
            ("sigma_ratio", "<=", 0.5, "σ(факт − план) с хеджем / без хеджа")],
    FUNDING: [("days", ">=", 60, "дней бумаги"), ("payments", ">=", 90, "выплат фандинга"),
              ("apr_over_earn_pp", ">=", 3, "чистая APR − ставка earn, п.п."),
              ("max_drawdown", "<=", 0.02, "просадка")],
    DIRECTIONAL: [("days", ">=", 90, "дней бумаги"), ("trades", ">=", 50, "сделок на бумаге"),
                  ("in_backtest_ci90", "==", True, "результат бумаги внутри 90% ДИ бэктеста")],
    MAKER: [("days", ">=", 30, "дней бумаги"), ("net_after_fee", ">", 0, "итог после комиссии 0.3%")],
}
BUTTON_TO_AUTO = {
    HEDGE: [("days", ">=", 21, "дней реальной торговли"), ("count", ">=", 30, "реальных хеджей"),
            ("model_divergence_pp", "<=", 0.05, "расхождение с моделью, п.п.")],
    FUNDING: [("days", ">=", 30, "дней реальной торговли"), ("cycles", ">=", 20, "циклов"),
              ("within_paper_1sigma", "==", True, "результат в пределах бумаги ± 1σ")],
    DIRECTIONAL: [("days", ">=", 60, "дней реальной торговли"), ("trades", ">=", 30, "реальных сделок"),
                  ("max_drawdown", "<", 0.10, "просадка (стоп автомата — 10%)")],
    MAKER: [("confirmed_edits", ">=", 100, "подтверждённых правок"), ("corridor_exits", "==", 0, "выходов из коридора")],
}
AUTO_COMMON = [("unknown_over_10min", "==", 0, "ордеров unknown дольше 10 мин"),
               ("limit_violations", "==", 0, "нарушений лимитов")]
# «Сильный бэктест» — условие minlot и укороченной бумаги; пороги — ровно из плана: хедж и фандинг — пороги
# «бумага → кнопка» (хедж ≥ 50 хеджей / 14 дней, коэффициент 0.9–1.1 в ≥ 95%, стоимость ≤ 0.6 × запаса на курс, σ ≤ 0.5;
# фандинг ≥ 60 дней / 90 выплат, APR ≥ earn + 3 п.п., просадка ≤ 2%), направленная — бэктест плана (≥ 12 мес. вне
# выборки, ≥ 200 сделок, Sharpe ≥ 1, PF ≥ 1.2, лучше случайных входов p < 0.05).
BACKTEST_STRONG = {
    HEDGE: list(PAPER_TO_BUTTON[HEDGE]),
    FUNDING: list(PAPER_TO_BUTTON[FUNDING]),
    DIRECTIONAL: [("months_oos", ">=", 12, "месяцев вне выборки"), ("trades", ">=", 200, "сделок"),
                  ("sharpe", ">=", 1, "Sharpe"), ("profit_factor", ">=", 1.2, "profit factor"),
                  ("p_value_vs_random", "<", 0.05, "p-value против случайных входов")],
}
# Укороченная бумага (только с флагом владельца): меняется лишь срок — 14 дней (направленная 30); минимумы количества
# (хеджей, выплат, сделок) и условия качества — как в плане: фандингу 90 выплат всё равно нужно (~30 дней при 3 в сутки).
SHORT_PAPER = {
    HEDGE: {"days": 14},
    FUNDING: {"days": 14},
    DIRECTIONAL: {"days": 30},
}
_TOKEN = object()   # только load_backtest создаёт годный Backtest


class Backtest:
    """Статистика бэктеста одной стратегии, загруженная из локального файла владельца (load_backtest)."""
    __slots__ = ("strategy", "stats", "research_sha", "generated_at", "path", "_token")

    def __init__(self, strategy, stats, research_sha, generated_at, path, token):
        self.strategy, self.stats, self.research_sha = strategy, stats, research_sha
        self.generated_at, self.path, self._token = generated_at, path, token


def _loaded(backtest, strategy):
    return isinstance(backtest, Backtest) and backtest._token is _TOKEN and backtest.strategy == strategy \
        and isinstance(backtest.stats, dict)


def research_sha(root=None):
    """sha256 кода research/ (все .py по имени; переводы строк нормализованы) — к нему привязана статистика бэктеста
    (не подпись: sha открыт, его может вписать любой, кто пишет в data/)."""
    folder = os.path.join(root or ROOT, RESEARCH_DIR)
    names = sorted(n for n in os.listdir(folder) if n.endswith(".py"))
    if not names:
        raise ValueError("в research/ нет кода")
    h = hashlib.sha256()
    for name in names:
        with open(os.path.join(folder, name), "rb") as f:
            data = f.read().replace(b"\r\n", b"\n")
        h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def _gitdir(root):
    """Папка git бота (.git — папка или файл «gitdir: …» у worktree) или None."""
    dotgit = os.path.join(root, ".git")
    try:
        if os.path.isdir(dotgit):
            return dotgit
        if os.path.isfile(dotgit):
            with open(dotgit, encoding="utf-8") as f:
                text = f.read().strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = text[len("gitdir:"):].strip()
            return gitdir if os.path.isabs(gitdir) else os.path.normpath(os.path.join(root, gitdir))
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _git_index(root):
    """Байты индекса git папки бота или None — не прочитать."""
    gitdir = _gitdir(root)
    if gitdir is None:
        return None
    try:
        with open(os.path.join(gitdir, "index"), "rb") as f:
            return f.read()
    except OSError:
        return None


def _varint(data, pos):
    """Число git «offset varint» (индекс v4: сколько байт отрезать от предыдущего пути) → (число, новая позиция)."""
    b = data[pos]
    pos += 1
    value = b & 0x7F
    while b & 0x80:
        value += 1
        b = data[pos]
        pos += 1
        value = (value << 7) + (b & 0x7F)
    return value, pos


def index_entries(data, gitdir=None):
    """Пути индекса git → (set путей, set sparse-папок) или ValueError — не разобрать. Формат git (index-format.txt):
    «DIRC», версия 2/3/4, число записей; запись — 62 байта stat + sha + флаги (у v3/v4 с флагом extended — ещё 2),
    затем путь: v2/v3 — строка с NUL и выравниванием до 8 байт, v4 — сжатие префикса (varint + остаток до NUL); в конце
    SHA-1 содержимого (нули — index.skipHash), не сошлась — не SHA-1 индекс, ValueError. Split index (расширение
    «link») — записи и из sharedindex.<sha> (объединение: так строже). Sparse-папка (mode 040000) — всё под ней
    считается в индексе."""
    if len(data) < 32 or data[:4] != b"DIRC":
        raise ValueError("не индекс git")
    tail = data[-20:]
    if tail != b"\0" * 20 and tail != hashlib.sha1(data[:-20]).digest():   # noqa: S324 — формат git, не защита
        # не SHA-1 индекс (репозиторий sha256 и т. п.) или битый файл — разбирать по чужой раскладке нельзя
        raise ValueError("контрольная сумма индекса git не сошлась")
    version, count = int.from_bytes(data[4:8], "big"), int.from_bytes(data[8:12], "big")
    if version not in (2, 3, 4):
        raise ValueError(f"индекс git версии {version} — не разобрать")
    paths, dirs, pos, prev = set(), set(), 12, b""
    for _ in range(count):
        start = pos
        mode = int.from_bytes(data[pos + 24:pos + 28], "big")
        flags = int.from_bytes(data[pos + 60:pos + 62], "big")
        pos += 62
        if flags & 0x4000:
            if version < 3:
                raise ValueError("extended-флаг в индексе v2")
            pos += 2
        if version == 4:
            strip, pos = _varint(data, pos)
            end = data.index(b"\0", pos)
            if strip > len(prev):
                raise ValueError("битое сжатие пути в индексе v4")
            name = prev[:len(prev) - strip] + data[pos:end]
            pos = end + 1
        else:
            end = data.index(b"\0", pos)
            name = data[pos:end]
            pos = start + ((end - start) // 8 + 1) * 8   # NUL + выравнивание: длина записи кратна 8
        if pos > len(data) - 20:
            raise ValueError("индекс git обрезан")
        prev = name
        path = name.decode("utf-8", "surrogateescape")
        (dirs if mode == 0o040000 else paths).add(path.rstrip("/"))
    while pos + 8 <= len(data) - 20:   # расширения до контрольной суммы
        sig, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "big")
        body = data[pos + 8:pos + 8 + size]
        if len(body) != size:
            raise ValueError("расширение индекса git обрезано")
        if sig == b"link":
            if gitdir is None or len(body) < 20:
                raise ValueError("split index без папки git — не разобрать")
            shared = body[:20].hex()
            if shared != "0" * 40:
                with open(os.path.join(gitdir, f"sharedindex.{shared}"), "rb") as f:
                    more, more_dirs = index_entries(f.read(), gitdir)
                paths |= more
                dirs |= more_dirs
        pos += 8 + size
    return paths, dirs


def _tracked(root, rel):
    """rel в индексе git бота: True / False или None — индекс не прочитать или не разобрать."""
    gitdir = _gitdir(root)
    index = _git_index(root)
    if index is None:
        return None
    try:
        paths, dirs = index_entries(index, gitdir)
    except (ValueError, IndexError, OSError):
        return None
    return rel in paths or any(rel == d or rel.startswith(d + "/") for d in dirs)


def load_backtest(strategy, path=None, root=None, now=None):
    """Статистика бэктеста стратегии из локального файла владельца → (Backtest, "") или (None, причина). Файл:
    {"version": 1, "generated_at": unix-время, "research_sha": research_sha(), "strategies": {стратегия: {...}}}.
    Годен, только если лежит внутри data/ бота, его нет в индексе git, sha кода research/ совпадает с нынешним и время
    создания не из будущего."""
    root = root or ROOT
    path = path or os.path.join(root, "data", BACKTEST_FILE)
    real, data_dir = os.path.realpath(path), os.path.realpath(os.path.join(root, "data"))
    if not real.startswith(data_dir + os.sep):
        return None, "статистика бэктеста — только из локального файла в data/ бота (не из репозитория)"
    rel = os.path.relpath(real, os.path.realpath(root)).replace(os.sep, "/")
    tracked = _tracked(root, rel)
    if tracked is None:
        return None, ("не проверить, что файл статистики не из git (нет индекса git или он не разобран) — порог не "
                      "пройден")
    if tracked:
        return None, f"{rel} есть в git — закоммиченная статистика бэктеста порог не проходит"
    try:
        with open(real, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None, f"нет локального файла статистики бэктеста ({rel})"
    except (OSError, ValueError):
        return None, f"{rel}: не прочитан или битый JSON"
    if not isinstance(raw, dict) or raw.get("version") != BACKTEST_VERSION:
        return None, f"{rel}: не та версия формата"
    try:
        sha = research_sha(root)
    except (OSError, ValueError) as e:
        return None, f"код research/ не прочитан: {e}"
    if raw.get("research_sha") != sha:
        return None, "статистика посчитана другим кодом research/ (sha не совпал) — пересчитайте бэктест"
    ts, now = raw.get("generated_at"), time.time() if now is None else now
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not ts <= now + 300:
        return None, f"{rel}: нет или неверное время создания"
    stats = (raw.get("strategies") or {}).get(strategy) if isinstance(raw.get("strategies"), dict) else None
    if not isinstance(stats, dict):
        return None, f"{rel}: нет статистики стратегии {strategy}"
    return Backtest(strategy, dict(stats), sha, ts, real, _TOKEN), ""


def flags_from_file(path):
    """При старте бота: флаг владельца TRADING_SHORT_PAPER из .env на ПК (окружение процесса не считается). Включён,
    только если в файле есть такая строка и ВСЕ такие строки равны 1 (разбор — как у switch.switch_from_file)."""
    vals = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lstrip("﻿").strip().upper() == SHORT_PAPER_FLAG:
                        vals.append(v.split(" #")[0].strip())
    except (OSError, ValueError):
        vals = []
    _FLAGS["short_paper"] = bool(vals) and all(v == "1" for v in vals)
    return dict(_FLAGS)


def short_paper_enabled():
    return _FLAGS["short_paper"] is True


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _holds(value, op, threshold):
    if op == "==" and isinstance(threshold, bool):
        return value is threshold
    v = _num(value)
    if v is None:
        return False
    return {">=": v >= threshold, "<=": v <= threshold, ">": v > threshold, "<": v < threshold,
            "==": v == threshold}[op]


def evaluate(rules, stats, overrides=None):
    """Gate(passed, причины) по правилам; overrides — {ключ: другой порог} (укороченная бумага)."""
    stats = stats if isinstance(stats, dict) else {}
    failures = []
    for key, op, threshold, what in rules:
        threshold = (overrides or {}).get(key, threshold)
        if key not in stats:
            failures.append(f"нет данных: {what} ({key})")
        elif not _holds(stats[key], op, threshold):
            failures.append(f"{what}: {stats[key]!r}, нужно {op} {threshold}")
    return Gate(not failures, tuple(failures))


def backtest_strong(strategy, backtest):
    """Сильный ли бэктест. backtest — только Backtest из load_backtest (локальный файл владельца); словарь и прочее —
    не бэктест."""
    rules = BACKTEST_STRONG.get(strategy)
    if rules is None:
        return Gate(False, (f"{strategy}: бэктест не заменяет бумагу",))
    if not _loaded(backtest, strategy):
        return Gate(False, (f"статистика бэктеста — только из локального файла data/{BACKTEST_FILE} (load_backtest)",))
    return evaluate(rules, backtest.stats)


def paper_to_button(strategy, paper, backtest=None):
    """Порог «бумага → кнопка» (полные потолки). Сильный бэктест и флаг владельца TRADING_SHORT_PAPER=1 — укороченная
    бумага (SHORT_PAPER: только срок), остальное — как в плане. Направленной без сильного бэктеста кнопка не положена
    вовсе (план: бэктест — часть порога)."""
    if strategy not in PAPER_TO_BUTTON:
        return Gate(False, (f"стратегия {strategy!r} неизвестна",))
    bt = backtest_strong(strategy, backtest) if backtest is not None or strategy == DIRECTIONAL else None
    if strategy == DIRECTIONAL and not bt.passed:
        return Gate(False, ("бэктест не прошёл: " + "; ".join(bt.failures),))
    overrides = SHORT_PAPER.get(strategy) if bt is not None and bt.passed and short_paper_enabled() else None
    return evaluate(PAPER_TO_BUTTON[strategy], paper, overrides)


def button_to_auto(strategy, live):
    if strategy not in BUTTON_TO_AUTO:
        return Gate(False, (f"стратегия {strategy!r} неизвестна",))
    return evaluate(BUTTON_TO_AUTO[strategy] + AUTO_COMMON, live)


def max_mode(strategy, paper=None, backtest=None, live=None):
    """Самый рискованный режим, который разрешают пороги: auto / confirm / minlot / paper."""
    confirm = paper_to_button(strategy, paper, backtest)
    if confirm.passed:
        return "auto" if button_to_auto(strategy, live).passed else "confirm"
    if backtest is not None and backtest_strong(strategy, backtest).passed:
        return "minlot"
    return "paper"


def allow(strategy, requested, paper=None, backtest=None, live=None):
    """Разрешён ли режим requested: Gate. auto без пройденных порогов — всегда отказ."""
    if requested not in MODES:
        return Gate(False, (f"режим {requested!r} неизвестен",))
    top = max_mode(strategy, paper, backtest, live)
    if MODES.index(requested) <= MODES.index(top):
        return Gate(True, ())
    why = []
    if requested in ("confirm", "auto"):
        why += paper_to_button(strategy, paper, backtest).failures
    if requested == "auto":
        why += button_to_auto(strategy, live).failures
    if requested == "minlot":
        why += backtest_strong(strategy, backtest).failures if backtest is not None else ("нет бэктеста",)
    return Gate(False, (f"пороги разрешают только {top}",) + tuple(why))


# --- математика статистики (float) ---

def mean(xs):
    xs = [float(x) for x in xs]
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    """Выборочное σ (n − 1); меньше двух точек — None."""
    xs = [float(x) for x in xs]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(returns, periods_per_year):
    """Годовой Sharpe без безрисковой ставки: mean/σ × √периодов; σ = 0 или мало точек — None."""
    m, s = mean(returns), stdev(returns)
    if m is None or not s:
        return None
    return m / s * math.sqrt(periods_per_year)


def profit_factor(pnls):
    """Сумма прибылей / |сумма убытков|; убытков нет — inf (если есть прибыль), сделок нет — None."""
    wins = sum(float(p) for p in pnls if float(p) > 0)
    losses = -sum(float(p) for p in pnls if float(p) < 0)
    if losses == 0:
        return math.inf if wins > 0 else None
    return wins / losses


def max_drawdown(equity):
    """Максимальная просадка доли от пика по кривой капитала (значения > 0)."""
    peak, worst = None, 0.0
    for e in equity:
        e = float(e)
        if e <= 0:
            raise ValueError("капитал должен быть > 0")
        peak = e if peak is None else max(peak, e)
        worst = max(worst, (peak - e) / peak)
    return worst


def ci90(bt_mean, bt_std, n):
    """90% ДИ среднего по n сделкам при распределении бэктеста: mean ± 1.645·σ/√n."""
    half = Z90 * float(bt_std) / math.sqrt(n)
    return float(bt_mean) - half, float(bt_mean) + half


def within_ci90(paper_mean, bt_mean, bt_std, n_paper):
    """Средний результат n сделок бумаги внутри 90% ДИ бэктеста (для порога направленной)."""
    if n_paper < 1 or bt_std is None:
        return False
    lo, hi = ci90(bt_mean, bt_std, n_paper)
    return lo <= float(paper_mean) <= hi


def within_sigma(live_mean, paper_mean, paper_std):
    """Результат реальной торговли в пределах бумаги ± 1σ (для порога фандинга)."""
    if paper_std is None:
        return False
    return abs(float(live_mean) - float(paper_mean)) <= float(paper_std)
