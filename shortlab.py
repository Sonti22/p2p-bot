"""Independent paper research journal and bounded background learning.

Neither this journal nor the fitted model is read by shorts.tick to place entries.
Only one recording job and one learning job can run; market processing never waits.
"""
import asyncio
import csv
import copy
import hashlib
import html
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import quote

import shorts
import shortlogic

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, 'data', 'short_lab.db')
MODEL_PATH = os.path.join(ROOT, 'data', 'short_learning_model.json')
VERSION = 'short-lab-v1'
TRAIN_EVERY = 6 * 3600
MODEL_MAX_AGE = 7 * 86400


def isolated_paths(source=None, path=None, model_path=None):
    paths = tuple(os.path.realpath(os.path.abspath(p)) for p in
                  (source or shorts.DB_PATH, path or DB_PATH, model_path or MODEL_PATH))
    for i, left in enumerate(paths):
        for right in paths[i + 1:]:
            same = os.path.normcase(left) == os.path.normcase(right)
            if os.path.exists(left) and os.path.exists(right):
                same = same or os.path.samefile(left, right)
            if same:
                raise ValueError('Журнал позиций, журнал исследования и модель должны иметь разные пути')
    return paths


def escaped(value, maximum=50):
    raw = str(value)
    return html.escape(raw[:maximum]) + ('…' if len(raw) > maximum else '')


@contextmanager
def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.executescript('''
          CREATE TABLE IF NOT EXISTS decisions (
            symbol TEXT, candle INTEGER, version TEXT, first_ts REAL, last_ts REAL,
            observations INTEGER, state TEXT, PRIMARY KEY(symbol,candle,version));
          CREATE TABLE IF NOT EXISTS transitions (
            symbol TEXT, candle INTEGER, version TEXT, ts REAL, signature TEXT, state TEXT,
            PRIMARY KEY(symbol,candle,version,signature));
          CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY CHECK(id=1), ts REAL, state TEXT);
          CREATE TABLE IF NOT EXISTS training (id INTEGER PRIMARY KEY CHECK(id=1), ts REAL, state TEXT);
        ''')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def record(markets, diagnostics, now, path=None, artifact=None):
    """One record per symbol/closed-candle window, plus meaningful state transitions."""
    with connect(path) as con:
        for sym, market in sorted(markets.items()):
            observed = market.get('observed_at', now)
            at = float(observed) if observed is not None and shorts.dec(observed).is_finite() and 0 <= shorts.dec(observed) <= shorts.dec(now) else now
            decision = shortlogic.explain(market, at)
            decision['recorded_at'] = float(now)
            if artifact is not None:
                import shortlearn
                decision['prediction'] = shortlearn.predict(artifact, market, at)
            candle = int(at // 900) * 900
            raw = shorts.dump(decision)
            con.execute('''INSERT INTO decisions VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(symbol,candle,version) DO UPDATE SET
                           last_ts=excluded.last_ts, observations=observations+1, state=excluded.state''',
                        (sym, candle, shortlogic.VERSION, at, at, 1, raw))
            meaningful = {'gates': decision['gates'], 'hypotheses': decision['hypotheses'],
                          'entry_ready': decision['entry_ready']}
            signature = hashlib.sha256(shorts.dump(meaningful).encode()).hexdigest()
            con.execute('INSERT OR IGNORE INTO transitions VALUES(?,?,?,?,?,?)',
                        (sym, candle, shortlogic.VERSION, at, signature, raw))
        con.execute('INSERT OR REPLACE INTO runtime VALUES(1,?,?)',
                    (now, shorts.dump(diagnostics)))


def save_training(result, now, path=None):
    with connect(path) as con:
        con.execute('INSERT OR REPLACE INTO training VALUES(1,?,?)', (now, shorts.dump(result)))


def read(path=None):
    path = path or DB_PATH
    if not os.path.exists(path):
        return {'decisions': [], 'count': 0, 'runtime': {}, 'training': None}
    con = sqlite3.connect('file:' + quote(os.path.abspath(path).replace('\\', '/'), safe='/:') + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute('SELECT state FROM decisions ORDER BY last_ts DESC LIMIT 5').fetchall()
        runtime = con.execute('SELECT ts,state FROM runtime WHERE id=1').fetchone()
        training = con.execute('SELECT ts,state FROM training WHERE id=1').fetchone()
        return {'decisions': [json.loads(r[0]) for r in rows],
                'count': con.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],
                'runtime': dict(json.loads(runtime['state']), recorded_at=runtime['ts']) if runtime else {},
                'training': dict(json.loads(training['state']), recorded_at=training['ts']) if training else None}
    finally:
        con.close()


def report(section='learning', path=None):
    data = read(path)
    if section == 'decisions':
        blocks = ['🔎 <b>Проверка условий кандидатов</b>', f"Записанных окон наблюдения: {data['count']}",
                  'Условия показаны на момент получения данных. Фактические входы и выходы находятся в журнале позиций.']
        for row in data['decisions']:
            lines = ['<b>' + html.escape(row['symbol']) + '</b>',
                     'Допуск данных: ' + ('пройден' if not row['gates'] else 'не пройден')]
            lines.extend('• ' + escaped(g) for g in row['gates'][:4])
            for h in row['hypotheses']:
                lines.append(html.escape(h['name']) + ': ' + ('подтверждение есть' if h['confirmed'] else escaped(h['reason'] or 'нет подтверждения', 70)))
            lines.append('Вход основной гипотезы: ' + ('данные и сигнал допускают дальнейшую проверку размера' if row['entry_ready'] else 'ожидание / отказ'))
            prediction = row.get('prediction')
            if prediction:
                if prediction.get('available'):
                    lines.append(f"Теневая оценка положительного учебного исхода через час: {prediction['probability'] * 100:.1f}%")
                    lines.append('Эта оценка не управляет входом и не является вероятностью прибыли рабочей стратегии.')
                else:
                    lines.append('Теневая оценка недоступна: ' + escaped(prediction.get('reason', 'нет подтверждённой модели')))
            blocks.append('\n'.join(lines))
        if not data['decisions']:
            blocks.append('Новый журнал решений пока пуст. Заполняется после запуска обновлённой виртуальной стратегии.')
        return '\n\n'.join(blocks)
    if section == 'feed':
        diagnostics = data['runtime']
        if not diagnostics:
            return '⚡ <b>Качество и скорость данных</b>\n\nНаблюдений нового сборщика пока нет.'
        age = max(0, time.time() - diagnostics['recorded_at'])
        blocks = ['⚡ <b>Качество и скорость данных</b>', f'Возраст отчёта: {age:.1f} с',
                  'Длительность последнего обновления: ' + str(diagnostics.get('refresh_seconds', diagnostics.get('refresh_duration', 'не измерена'))) + ' с',
                  'Запросов: ' + str(diagnostics.get('requests', 0)) + '; повторов: ' + str(diagnostics.get('retries', 0)),
                  'Ошибок запросов: ' + str(diagnostics.get('request_errors', 0)) + '; тайм-аутов: ' + str(diagnostics.get('timeouts', 0)),
                  'Пауза после ограничения биржи: ' + str(diagnostics.get('cooldown_seconds', 0)) + ' с',
                  'Часы: ' + html.escape(str(diagnostics.get('clock_status', 'не проверены'))) +
                  '; сдвиг: ' + str(diagnostics.get('clock_offset_seconds', '?')) + ' с',
                  'Свежесть и непрерывность обязательны; старые данные не получают новую временную метку.']
        for key, label in (('lab_error', 'Ошибка исследования'), ('model_error', 'Оценки модели')):
            if diagnostics.get(key):
                blocks.append(label + ': ' + escaped(diagnostics[key], 180))
        for symbol, detail in list(diagnostics.get('sampling', {}).items())[:10]:
            blocks.append('<b>' + html.escape(symbol) + '</b>\n'
                          'Прогрев mark: ' + html.escape(str(detail.get('seconds', detail.get('window_seconds', '?')))) + ' с из 60\n'
                          'Последняя цена: ' + html.escape(str(detail.get('age', detail.get('latest_age', '?')))) + ' с назад\n'
                          'Причина сброса: ' + html.escape(str(detail.get('reset_reason') or 'нет')))
        return '\n\n'.join(blocks)
    training = data['training']
    blocks = ['🧠 <b>Обучение · теневая модель</b>',
              'Модель изучает наблюдаемые исходы после комиссий и финансирования. '
              'Её оценки не открывают позиции и не меняют риск.',
              f"Записанных окон решений: {data['count']}"]
    if training is None:
        blocks.append('Обучение ещё не выполнено. Фоновая проверка запускается после активации обновлённого бота, затем не чаще раза в 6 часов.')
    else:
        blocks.append('Последняя проверка: ' + time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(training['recorded_at'])))
        blocks.append('Статус: ' + ('модель обучена для теневого наблюдения' if training.get('ready') else 'данных или качества проверки пока недостаточно'))
        if training.get('reason'):
            blocks.append(html.escape(str(training['reason'])[:400]))
        if training.get('trained'):
            import shortlearn
            blocks.append(shortlearn.report(training))
        diagnostics = training.get('dataset_diagnostics', training.get('diagnostics', {}))
        coverage = training.get('evaluation', {}).get('label_coverage', {}).get('all') or diagnostics.get('label_coverage')
        if coverage:
            fraction = coverage.get('known_fraction', 0)
            blocks.append('<b>Полнота учебных исходов</b>\n'
                          f"Проверено: {coverage.get('known', 0)} из {coverage.get('attempted', 0)}\n"
                          f'Известных исходов: {fraction * 100:.1f}% · требуется ≥80%\n'
                          'Пропуски остаются в оценке качества данных.')
        if diagnostics:
            labels = {'observations': 'Рыночных снимков', 'valid_labels': 'Известных исходов', 'valid': 'Известных исходов',
                      'invalid': 'Непроверенных исходов', 'days': 'Дней истории',
                      'rows': 'Учебных примеров', 'unknown': 'Неизвестных исходов'}
            lines = [labels[key] + ': ' + html.escape(str(diagnostics[key])) for key in labels if key in diagnostics]
            if lines:
                blocks.append('<b>Покрытие данных</b>\n' + '\n'.join(lines))
            unknown = diagnostics.get('unknown_labels', {})
            if unknown:
                blocks.append('Непроверенных исходов: ' + str(sum(unknown.values())) + '\n'
                              'Причины проверяются отдельно; неизвестный результат не заменяется нулём.')
    blocks.append('Хронологические обучение, валидация и итоговая проверка разделены. '
                  'Пересекающиеся периоды исходов исключаются. Неизвестный результат не становится выигрышем.\n'
                  '/shorts knowledge · /shorts decisions · /shorts feed · /shorts labexport')
    return '\n\n'.join(blocks)


def export(destination, path=None):
    with connect(path) as con, open(destination, 'w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['symbol', 'window_utc', 'version', 'first_observed', 'last_observed', 'observations', 'explanation'])
        for row in con.execute('SELECT * FROM decisions ORDER BY candle,symbol'):
            writer.writerow(tuple(row))


def train(source=None, path=None, model_path=None):
    """Thread-only job; bounded source rows, no positions or balance changes."""
    import shortlearn
    source, path, model_path = isolated_paths(source, path, model_path)
    dataset = shortlearn.build_dataset(source, limit=5000)
    result = shortlearn.fit(dataset)
    if result.get('ready'):
        shortlearn.save(result, model_path or MODEL_PATH)
    save_training(result, time.time(), path)
    return result


class Worker:
    def __init__(self, source=None, path=None, model_path=None):
        self.source, self.path, self.model_path = isolated_paths(source, path, model_path)
        self.record_task = self.train_task = None
        self.next_train = 0
        self.skipped_records = 0
        self.last_error = ''
        try:
            previous = read(self.path).get('training')
            if previous and 0 <= time.time() - previous['recorded_at'] <= TRAIN_EVERY:
                self.next_train = time.monotonic() + TRAIN_EVERY - (time.time() - previous['recorded_at'])
        except (OSError, ValueError, sqlite3.Error):
            self.last_error = 'Не удалось прочитать время последнего обучения'

    def submit(self, markets, diagnostics, now):
        # Detached copies prevent an async collector from modifying a pending thread's input.
        if self.record_task is None or self.record_task.done():
            copied = copy.deepcopy(markets)
            info = copy.deepcopy(diagnostics)
            self.record_task = asyncio.create_task(self._record(copied, info, now))
        else:
            self.skipped_records += 1
        if time.monotonic() >= self.next_train and (self.train_task is None or self.train_task.done()):
            self.next_train = time.monotonic() + TRAIN_EVERY
            self.train_task = asyncio.create_task(self._train())

    async def _record(self, markets, diagnostics, now):
        try:
            diagnostics['lab_skipped_records'] = self.skipped_records
            if self.last_error:
                diagnostics['lab_error'] = self.last_error
            await asyncio.to_thread(self._record_scored, markets, diagnostics, now)
        except Exception as exc:
            self.last_error = 'Журнал обучения: ' + type(exc).__name__

    def _record_scored(self, markets, diagnostics, now):
        import shortlearn
        artifact = None
        if os.path.exists(self.model_path or MODEL_PATH):
            try:
                loaded = shortlearn.load(self.model_path or MODEL_PATH)
                age = time.time() - loaded['created_at']
                outcome_at = loaded.get('evaluation', {}).get('partitions', {}).get('test', {}).get('end')
                outcome_age = time.time() - outcome_at if outcome_at is not None else MODEL_MAX_AGE + 1
                latest = read(self.path).get('training')
                current = latest is not None and latest.get('ready')
                artifact = loaded if (loaded.get('ready') and current and 0 <= age <= MODEL_MAX_AGE
                                      and 0 <= outcome_age <= MODEL_MAX_AGE) else None
                if loaded.get('ready') and artifact is None:
                    diagnostics['model_error'] = 'Модель или данные устарели / последняя проверка не пройдена; оценки отключены'
            except (OSError, ValueError, TypeError):
                diagnostics['model_error'] = 'Сохранённая модель не прошла проверку; оценки отключены'
        record(markets, diagnostics, now, self.path, artifact)

    async def _train(self):
        try:
            await asyncio.to_thread(train, self.source, self.path, self.model_path)
        except Exception as exc:
            self.last_error = 'Обучение: ' + type(exc).__name__
            try:
                await asyncio.to_thread(save_training, {'ready': False, 'reason': self.last_error}, time.time(), self.path)
            except Exception:
                self.last_error += '; отчёт не сохранён'

    async def close(self):
        # Finish already-started thread work before graceful shutdown; no task backlog.
        tasks = [task for task in (self.record_task, self.train_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
