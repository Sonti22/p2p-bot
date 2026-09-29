"""Разбор полей выгрузки BestChange (info.zip) на ПК владельца — без сети: файл скачивает сам владелец (тот же адрес,
что берёт бот, p2p.BC_URL), путь — аргументом. Нужен для пункта ROADMAP «Метка надёжности сигнала, часть 3»: есть ли в
строке обменника (bm_exch.dat) признак, что BestChange его ограничивал (схема «белый треугольник»), — из облака
выгрузка недоступна (хост не в разрешённых). Бот сейчас читает из bm_exch.dat только id и имя (p2p._bc_parse).

    python scripts/bc_fields.py info.zip [--file bm_exch.dat] [--samples 8]

По каждой колонке файла: сколько строк её заполняют, сколько разных значений и самые частые из них (со счётчиком).
Флаг — обычно колонка с парой значений (0/1), где редкое значение у немногих обменников; их имена печатаются рядом."""
import argparse
import collections
import io
import sys
import zipfile


def columns(data, name="bm_exch.dat"):
    """Строки файла из архива (cp1251, «;»-разделитель) → [[значения колонки i по строкам]]."""
    z = zipfile.ZipFile(io.BytesIO(data))
    rows = [line.split(";") for line in z.read(name).decode("cp1251").splitlines() if line.strip()]
    width = max((len(r) for r in rows), default=0)
    return rows, [[r[i] if i < len(r) else "" for r in rows] for i in range(width)]


def describe(rows, cols, samples=8, name_col=1):
    """Текст отчёта: колонка, заполнено, разных, частые значения; у колонок с 2–3 значениями — кто в редком."""
    out = [f"строк: {len(rows)}, колонок: {len(cols)}"]
    for i, col in enumerate(cols):
        filled = sum(1 for v in col if v != "")
        counts = collections.Counter(col)
        top = ", ".join(f"{v!r}×{n}" for v, n in counts.most_common(samples))
        out.append(f"[{i}] заполнено {filled}/{len(col)}, разных {len(counts)}: {top}")
        if 2 <= len(counts) <= 3 and i != name_col:
            rare, n = counts.most_common()[-1]
            if n <= max(1, len(col) // 10):
                who = [r[name_col] for r in rows if len(r) > max(i, name_col) and r[i] == rare][:samples]
                out.append(f"    редкое {rare!r} у {n}: {', '.join(who)}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Поля выгрузки BestChange (info.zip) — без сети")
    ap.add_argument("zip", help="путь к info.zip")
    ap.add_argument("--file", default="bm_exch.dat", help="файл внутри архива (bm_exch.dat, bm_rates.dat, bm_cy.dat)")
    ap.add_argument("--samples", type=int, default=8)
    args = ap.parse_args(argv)
    try:
        with open(args.zip, "rb") as f:
            data = f.read()
        rows, cols = columns(data, args.file)
    except (OSError, KeyError, zipfile.BadZipFile) as e:
        print(f"не прочитать {args.zip}: {e}", file=sys.stderr)
        return 2
    print(describe(rows, cols, args.samples))
    return 0


if __name__ == "__main__":
    sys.exit(main())
