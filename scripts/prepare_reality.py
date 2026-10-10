"""Consistent backup and opt-in to independent paper models. Bot must be stopped."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(source):
    with closing(sqlite3.connect('file:' + source.as_posix() + '?mode=ro', uri=True)) as con:
        h = hashlib.sha256()
        for line in con.iterdump():
            h.update(line.encode('utf-8'))
            h.update(b'\n')
        return h.hexdigest()


def prepare(root=ROOT, now=None):
    now = time.time() if now is None else now
    sources = list((root/'data').glob('*.db'))
    originals = {source: digest(source) for source in sources}
    backup = root/'data'/'backup'/('reality-' + str(time.time_ns()))
    backup.mkdir(parents=True)
    for source in sources:
        with closing(sqlite3.connect('file:' + source.as_posix() + '?mode=ro', uri=True)) as src, \
             closing(sqlite3.connect(str(backup/source.name))) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Backup integrity check failed')
    configs = []
    for source in (root/'.env', root/'data'/'paper_bank_profiles.json'):
        if source.exists():
            shutil.copy2(source, backup/source.name)
            configs.append((source, hashlib.sha256(source.read_bytes()).hexdigest()))
    if any(digest(source) != before for source, before in originals.items()) or any(
            hashlib.sha256(source.read_bytes()).hexdigest() != before for source, before in configs):
        raise RuntimeError('State changed during backup: stop the supervisor and bot first')
    result = {'backup': str(backup), 'source_journals': {p.name:h for p,h in originals.items()},
              'config_hashes': {p.name:h for p,h in configs}, 'created': now,
              'mode': 'prepared_only', 'real_operations': 'not_enabled'}
    (backup/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def prepare_model(root=ROOT, now=None):
    # Preparation does not enable any mode or rewrite settings.
    result = prepare(root,now)
    import reality
    reality.initialize(now, str(root/'data'/'rub_roundtrips.db'))
    costs = root/'data'/'reality_costs.json'
    if costs.exists():
        reality.configure_costs(json.loads(costs.read_text(encoding='utf-8')),
                                str(root/'data'/'rub_roundtrips.db'),now)
    result['mode'] = 'independent_virtual_cycle_prepared_not_enabled'
    (Path(result['backup'])/'preparation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__ == '__main__':
    if sys.argv[1:] == ['--prepare-bot-stopped']:
        result = prepare()
    elif sys.argv[1:] == ['--prepare-model-bot-stopped']:
        result = prepare_model()
    else:
        raise SystemExit('Usage: python scripts/prepare_reality.py --prepare-bot-stopped | --prepare-model-bot-stopped')
    print(json.dumps(result,ensure_ascii=False))
