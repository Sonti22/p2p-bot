"""Троттлинг одинаковых WARNING/ERROR в логе: миксин к logging.Handler, который не даёт сбою (сеть, диск) залить
bot.log и консоль тысячами одинаковых строк подряд — первая проходит сразу, дальше та же строка (логгер, уровень,
первые KEY_LEN символов текста) не чаще раза в WINDOW секунд, с числом подавленного в скобках у следующей строки.

Ограничение: счётчик пропущенного печатается только когда тот же ключ повторился ПОСЛЕ окна; если сбой прекратился,
последняя порция подавленных записей никогда не печатается (сознательно — без вывода при close() хендлера)."""
import copy
import logging
import time
from logging.handlers import RotatingFileHandler

WINDOW = 300
MAX_KEYS = 500
KEY_LEN = 200


class RepeatThrottle:
    def __init__(self, *args, window=WINDOW, min_level=logging.WARNING, clock=time.monotonic, max_keys=MAX_KEYS,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self._window = window
        self._min_level = min_level
        self._clock = clock
        self._max_keys = max_keys
        self._seen = {}   # ключ -> [время последнего пропущенного в вывод, число подавленных с тех пор]

    def _prune(self, now):
        if len(self._seen) <= self._max_keys:
            return
        for k, (last, _) in list(self._seen.items()):
            if len(self._seen) <= self._max_keys:
                return
            if now - last >= self._window:   # просроченные — раньше живых
                del self._seen[k]
        while len(self._seen) > self._max_keys:   # ещё сверх лимита — самые старые по порядку словаря
            del self._seen[next(iter(self._seen))]

    def emit(self, record):
        if record.levelno < self._min_level:
            super().emit(record)
            return
        try:
            key = (record.name, record.levelno, record.getMessage()[:KEY_LEN])
            now = self._clock()
            state = self._seen.get(key)
            if state is not None and 0 <= now - state[0] < self._window:
                state[1] += 1   # в окне — подавляем, счётчик растёт, порядок словаря не трогаем
                return
            skipped = state[1] if state is not None else 0
            last = state[0] if state is not None else now
            if state is not None:
                del self._seen[key]
            self._seen[key] = [now, 0]   # пере-вставка — в конец словаря: порядок = порядок последнего вывода
            self._prune(now)
        except Exception:
            super().emit(record)
            return
        if skipped:
            out = copy.copy(record)
            minutes = max(1, round((now - last) / 60))
            out.msg = f"{record.getMessage()} (ещё {skipped} таких же за {minutes} мин)"
            out.args = ()
            super().emit(out)
        else:
            super().emit(record)


class ThrottledFileHandler(RepeatThrottle, RotatingFileHandler):
    pass


class ThrottledStreamHandler(RepeatThrottle, logging.StreamHandler):
    pass
