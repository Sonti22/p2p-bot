"""Versioned explanations of the paper policy. No network or execution decisions.

These rules describe hypotheses and data requirements, not proven price forecasts.
All candle statistics exclude candles not closed at the observation time.
"""
import html
import statistics

import shorts

VERSION = 'short-explanations-v1'
HYPOTHESES = ('ema_retest', 'breakdown', 'wick_reversal')
NAMES = {'ema_retest': 'Пробой EMA20 и неудачный возврат',
         'breakdown': 'Пробой минимума шести свечей',
         'wick_reversal': 'Медвежий разворот с верхней тенью'}
KNOWLEDGE = (
    ('Перегрев', 'Рост и всплеск объёма дают повод наблюдать; сами по себе они не подтверждают падение.'),
    ('Разворот', 'Подтверждение проверяется только по закрытым свечам; будущие значения не используются.'),
    ('Продолжение роста', 'Обновление максимума и ускорение mark — причины отказа от нового шорта.'),
    ('Цена и исполнение', 'Mark запускает защиту, а фактическая цена закрытия зависит от предложений в стакане.'),
    ('Финансирование', 'Отрицательный funding может быть расходом шорта; ставка имеет собственный интервал.'),
    ('Защита капитала', 'Размер ограничивают стоп, выделенный капитал, расходы, глубина и минимум контракта.'),
    ('Качество данных', 'Разрыв наблюдений означает неизвестный исход защиты, а не успешный стоп.'),
    ('Обучение', 'Теневая модель оценивает зарегистрированные исходы и не меняет действующие входы или риск.'),
)


def explain(market, now):
    """Return bounded, JSON-compatible explanations; malformed inputs fail closed."""
    result = {'version': VERSION, 'symbol': str(market.get('symbol', '?'))[:40],
              'observed_at': float(now), 'gates': [], 'hypotheses': [], 'metrics': {},
              'entry_ready': False, 'model_role': 'shadow_only'}
    try:
        m = market
        if m.get('observed_at') is not None and shorts.dec(m['observed_at']) > shorts.dec(now):
            raise ValueError('время наблюдения находится в будущем')
        if not m.get('active'):
            result['gates'].append('Контракт не находится в активных торгах')
        if not m.get('candidate'):
            result['gates'].append('Не пройден первичный отбор контракта')
        protection = shorts.protection(m, now)
        if protection:
            result['gates'].append(protection)
        if not shorts.book_fresh(m, now):
            result['gates'].append('Нет свежего стакана для исполнения')
        if not m.get('fee_source'):
            result['gates'].append('Нет подтверждённой модели комиссии')
        for error in m.get('data_errors', [])[:5]:
            result['gates'].append(str(error)[:200])
        bars = shorts.contiguous(m.get('bars', []), 900, now, 35)
        hours = shorts.contiguous(m.get('hours', []), 3600, now, 21)
        if bars:
            means = shorts.ema([b[4] for b in bars])
            a = shorts.atr(bars)
            result['metrics'].update(atr_pct=str(a / shorts.dec(bars[-1][4]) * 100),
                                     ema_distance_pct=str((shorts.dec(bars[-1][4]) / means[-1] - 1) * 100),
                                     ema_direction='растёт' if means[-1] > means[-2] else 'снижается')
        if hours:
            median = statistics.median(shorts.dec(b[5]) for b in hours[-21:-1])
            result['metrics']['hour_volume_ratio'] = str(shorts.dec(hours[-1][5]) / median) if median > 0 else None
        bid, ask = shorts.dec(m['bids'][0][0]), shorts.dec(m['asks'][0][0])
        if bid <= 0 or ask < bid:
            raise ValueError('неверный стакан')
        result['metrics'].update(spread_pct=str((ask / bid - 1) * 100),
                                 growth_pct=str(shorts.dec(m['growth']) * 100),
                                 funding_pct=str(shorts.dec(m['funding_rate']) * 100),
                                 funding_interval_minutes=str(m['funding_interval']),
                                 book_age_seconds=float(now - m['book_ts']))
        for kind in HYPOTHESES:
            signal, reason = shorts.signal(m, now, kind)
            result['hypotheses'].append({'kind': kind, 'name': NAMES[kind],
                                        'confirmed': bool(signal), 'reason': reason,
                                        'stop': signal['stop'] if signal else None,
                                        'active': kind == 'ema_retest'})
        result['entry_ready'] = not result['gates'] and result['hypotheses'][0]['confirmed']
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
        result['gates'].append('Неполные или неверные рыночные данные: ' + type(exc).__name__)
        result['entry_ready'] = False
    return result


def guide():
    blocks = ['🧠 <b>Логика виртуальных шортов</b>']
    for title, explanation in KNOWLEDGE:
        blocks.append('<b>' + html.escape(title) + '</b>\n' + html.escape(explanation))
    blocks.append('Рабочая гипотеза: возврат к EMA20. Другие гипотезы сравниваются отдельно.\n'
                  'Плечо ≤2×; риск по стопу ≤0,5%; капитал позиции ≤2%; максимум две позиции.\n'
                  'Лимиты не повышаются ради активности. /shorts decisions · /shorts learning')
    return '\n\n'.join(blocks)
