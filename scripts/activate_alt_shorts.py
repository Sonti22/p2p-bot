"""Consistent backup and opt-in for public-data paper shorts. Run with bot stopped."""
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
        return hashlib.sha256('\n'.join(con.iterdump()).encode()).hexdigest()


def activate():
    backup = ROOT / 'data' / 'backup' / ('alt-shorts-' + str(time.time_ns()))
    backup.mkdir(parents=True)
    originals = {source: digest(source) for source in (ROOT/'data').glob('*.db')}
    for source in originals:
        with closing(sqlite3.connect('file:' + source.as_posix() + '?mode=ro', uri=True)) as con, \
             closing(sqlite3.connect(str(backup/source.name))) as dest:
            con.backup(dest)
    for source in (ROOT/'.env', ROOT/'data'/'paper_bank_profiles.json'):
        if source.exists():
            shutil.copy2(source, backup/source.name)
    if any(digest(source) != old for source, old in originals.items()):
        raise RuntimeError('Existing journals changed during backup; stop bot first')
    from bot import save_env
    save_env('ALT_SHORTS','1',path=str(ROOT/'.env'))
    result = {'backup':str(backup),'preserved_journals':len(originals),
              'enabled':'paper only; fresh observed RUB/USDT required to initialize'}
    (backup/'manifest.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    return result


if __name__ == '__main__':
    if sys.argv[1:] != ['--activate']:
        raise SystemExit('Usage: python scripts/activate_alt_shorts.py --activate (bot stopped)')
    print(json.dumps(activate(),ensure_ascii=False))
