"""Back up local journals, initialize alternative scenarios and enable their UI.

Run with the bot stopped. Does not approve protected Git updates or reset history.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import p2p
import portfolio
import scenarios


def activate():
    p2p.load_env(str(ROOT / '.env'))
    cfg = p2p.Config.from_env()
    backup = ROOT / 'data' / 'backup' / ('paper-scenarios-' + str(time.time_ns()))
    backup.mkdir(parents=True)
    for source in (ROOT / 'data').glob('*.db'):
        con = sqlite3.connect('file:' + source.as_posix() + '?mode=ro', uri=True)
        target = sqlite3.connect(str(backup / source.name))
        try:
            con.backup(target)
        finally:
            target.close()
            con.close()
    for source in (ROOT / '.env', ROOT / 'data' / 'paper_bank_profiles.json'):
        if source.exists():
            shutil.copy2(source, backup / source.name)
    before = portfolio.summary()
    results = {}
    for name in scenarios.VARIANTS:
        scenarios.maintain(name, cfg)
        results[name] = portfolio.summary(scenarios.path(name))
        if results[name]['initial'] != '50000.00':
            raise ValueError('Unexpected initial scenario capital')
    after = portfolio.summary()
    for key in ('initial', 'cash', 'reserved', 'realized', 'runs'):
        if before[key] != after[key]:
            raise ValueError('Verified portfolio changed during activation')
    from bot import save_env
    save_env('PAPER_SCENARIOS', '1', path=str(ROOT / '.env'))
    return {'backup': str(backup), 'verified_preserved': True,
            'scenarios': {k: {field: v[field] for field in ('initial', 'cash', 'bank_expenses')}
                          for k, v in results.items()}}


if __name__ == '__main__':
    if sys.argv[1:] != ['--activate']:
        raise SystemExit('Usage: python scripts/activate_paper_scenarios.py --activate (bot stopped)')
    print(json.dumps(activate(), ensure_ascii=False))
