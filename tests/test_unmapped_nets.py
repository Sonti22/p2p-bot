"""Журнал нераспознанных сетей (netstatus.UNMAPPED) и /nets: имя сети вне KNOWN_NETS запоминается как подсказка
владельцу; на расчёт маршрутов не влияет."""
import bot as B
import netstatus
import p2p
from helpers import arun

OPEN = {"dep": True, "wd": True, "fee": 1.0, "min": None}


def test_parse_keeps_unknown_name_and_apply_logs_it():
    j = {"code": "200000", "data": {"chains": [
        {"chainName": "TRC20", "isDepositEnabled": True, "isWithdrawEnabled": True, "withdrawalMinFee": "1"},
        {"chainName": "KAVA EVM", "isDepositEnabled": True, "isWithdrawEnabled": True, "withdrawalMinFee": "0.5"}]}}
    nets = netstatus._parse_kucoin(j)
    assert set(nets) == {"TRC20", "KAVA EVM"}
    netstatus._apply("KuCoin", "USDT", nets)
    rows = netstatus.unmapped()
    assert [(v, a, n) for v, a, n, _ in rows] == [("KuCoin", "USDT", "KAVA EVM")]
    netstatus._apply("KuCoin", "USDT", nets)
    assert netstatus.unmapped()[0][3]["seen"] == 2


def test_known_names_and_ticker_in_brackets_are_not_logged():
    netstatus._apply("HTX", "TON", {netstatus.normalize("Toncoin(TON)"): OPEN, netstatus.normalize("BSC"): OPEN})
    assert netstatus.unmapped() == []


def test_log_is_bounded_and_drops_least_recent(monkeypatch):
    monkeypatch.setattr(netstatus, "UNMAPPED_MAX", 3)
    for i in range(5):
        netstatus._note_unmapped("MEXC", "USDT", {f"NET{i}": OPEN}, now=1000.0 + i)
    assert [n for _, _, n, _ in netstatus.unmapped()] == ["NET4", "NET3", "NET2"]
    netstatus.reset()
    assert netstatus.unmapped() == []


def test_unmapped_net_does_not_change_route_choice():
    netstatus._apply("HTX", "USDT", {"TRC20": dict(OPEN, fee=1.0), "WEIRDCHAIN": dict(OPEN, fee=0.01)})
    assert p2p._withdraw(p2p.Config(), "HTX", "USDT", "", "BitPapa") == (1.0, "TRC20")


def test_nets_view_empty_and_rows():
    assert "Пока нет" in B.unmapped_nets_view(rows=[])
    rows = [("KuCoin", "USDT", "KAVA<EVM>", {"first": 0.0, "last": 940.0, "seen": 3})]
    text = B.unmapped_nets_view(rows=rows, now=1000.0)
    assert "• KuCoin USDT: <code>KAVA&lt;EVM&gt;</code> — 3 раз, последний 1 мин назад" in text


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def test_nets_command():
    netstatus._apply("MEXC", "USDT", {"OPTIMISM": OPEN})
    bot = Stub(p2p.Config())
    arun(bot.handle("/nets"))
    assert "<code>OPTIMISM</code>" in [p["text"] for m, p in bot.out if m == "sendMessage"][-1]
