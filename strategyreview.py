"""Read-only evidence review. Never changes execution policy or journal balances."""
import html
import time
from decimal import Decimal

import portfolio
import scenarios
import shorts
import shortresearch

VERSION = 'evidence-review-v1'


def p2p_evidence(summary):
    runs = sorted(summary['runs'], key=lambda r: (r['start'], r['id']))
    boundary = (runs[0]['start']+(runs[-1]['start']-runs[0]['start'])*.6) if runs else 0
    groups = {}
    for r in runs:
        key = (r['buy_ex'], r['sell_ex'], r.get('buy_asset',''), r.get('sell_asset',''), r.get('pay_kind',''))
        g = groups.setdefault(key, {'route': list(key), 'training': [], 'verification': [], 'open': 0, 'cancelled': 0})
        if r['stage'] not in ('done','cancelled'):
            g['open'] += 1
        elif r['stage'] == 'cancelled':
            g['cancelled'] += 1
        else:
            # Completion must precede the boundary to avoid using outcomes learned in the future.
            part = 'training' if r['stage_ts'] < boundary else 'verification'
            g[part].append((Decimal(r['realized']), Decimal(r.get('spent','0')),
                            max(0,r['stage_ts']-r['start'])))
    result = []
    for g in groups.values():
        item = {k:v for k,v in g.items() if k not in ('training','verification')}
        for part in ('training','verification'):
            rows = g[part]
            pnl = sum((x[0] for x in rows), Decimal(0))
            item[part] = {'completed': len(rows), 'realized': str(pnl),
                          'extra_cost_20bp': str(pnl-sum((x[1]*Decimal('.002') for x in rows),Decimal(0))),
                          'losses': sum(x[0]<0 for x in rows),
                          'mean_minutes': sum(x[2] for x in rows)/len(rows)/60 if rows else None}
        item['enough_observations'] = item['training']['completed']>=30 and item['verification']['completed']>=20
        result.append(item)
    return {'version': VERSION, 'boundary': boundary, 'routes': result,
            'net_realized': summary['net_realized'], 'service': summary['bank_expenses'],
            'verified_readiness': False,
            'limitations': ['Selected executed routes only; skipped opportunities not recorded',
                            'No counterfactual replay of P2P quotes or alternative bank allocation',
                            'Acceptance, permission and some fees remain scenario assumptions',
                            'Service is charged at account level, not duplicated per route',
                            '20bp cost stress is sensitivity analysis, not worse execution replay']}


def p2p_report(name='base'):
    if name not in scenarios.VARIANTS:
        raise ValueError('неизвестный сценарий')
    data = p2p_evidence(portfolio.summary(scenarios.path(name)))
    out = ['📚 <b>P2P · проверка маршрутов</b>', '',
           scenarios.LABELS[name]+' сценарий · варианты не суммируются',
           'Чистый реализованный результат: '+data['net_realized']+' ₽',
           'Обслуживание уже учтено: '+data['service']+' ₽', '',
           'Обучение: первые 60% периода. Проверка: последующие 40%.',
           'Для оценки одного маршрута нужно ≥30 обучающих и ≥20 проверочных кругов.', '']
    for g in data['routes'][:6]:
        a,b,coin,target,channel = g['route']
        out.extend(['<b>'+html.escape(a+' → '+b+' · '+coin+' → '+target)+'</b>',
                    'Обучающие круги: '+str(g['training']['completed']),
                    'Проверочные круги: '+str(g['verification']['completed']),
                    'Результат проверки: '+g['verification']['realized']+' ₽',
                    'При дополнительных расходах 0,2% покупки: '+g['verification']['extra_cost_20bp']+' ₽',
                    'Незавершённые / отменённые: '+str(g['open'])+' / '+str(g['cancelled']),
                    'Наблюдений достаточно: '+('да' if g['enough_observations'] else 'нет'), ''])
    out.extend(['<b>Достоверность</b>',
                'Результат сценарный. Непроверенные комиссии и допуск сделок не подтверждены.',
                'Пропущенные возможности не сохранены: выбрать доказанно лучший маршрут нельзя.',
                'Рабочие правила автоматически не меняются.'])
    return '\n'.join(out)


def shorts_report(path=None, now=None):
    now = time.time() if now is None else now
    s = shorts.status(path)
    if s is None:
        return shortresearch.summary(path)
    out = ['📚 <b>Шорты · данные и обучение</b>', '',
           'Возраст последней проверки рынка: '+str(round(max(0,now-s.get('last_tick',s['start']))))+' с',
           'Заработанный результат: '+s['realized']+' USDT',
           'Позиций открыто: '+str(sum(p['stage'] not in ('closed','funding') for p in s['positions'])), '',
           '<b>Причины отказов за всё время</b>']
    for reason,count in sorted(s.get('rejections',{}).items(),key=lambda x:x[1],reverse=True)[:5]:
        out.append(html.escape(reason)+': '+str(count))
    if s.get('last_error'):
        out.extend(['', '<b>Последняя проблема данных</b>',html.escape(s['last_error'][:500])])
    out.extend(['',shortresearch.summary(path)])
    return '\n'.join(out)
