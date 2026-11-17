"""The doviz_kuru tool over a saved TCMB sample; no network is used."""

from __future__ import annotations

from datetime import date

import pytest

from igris import fx_rates as fx
from igris.config import Settings, load_settings
from igris.tools import ToolError, ToolRegistry

# cut from the real bulletin of 12.11.2026 (read once by hand); the name "DANİMARKA" is in
# ISO-8859-9 bytes although the declaration says UTF-8, as the real file was
SAMPLE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<?xml-stylesheet type="text/xsl" href="isokur.xsl"?>\n'
    '<Tarih_Date Tarih="12.11.2026" Date="11/12/2026"  Bulten_No="2026/213" >\n'
    '<Currency CrossOrder="0" Kod="USD" CurrencyCode="USD"><Unit>1</Unit>'
    '<Isim>ABD DOLARI</Isim><CurrencyName>US DOLLAR</CurrencyName>'
    '<ForexBuying>50.1691</ForexBuying><ForexSelling>50.2595</ForexSelling>'
    '<BanknoteBuying>50.1340</BanknoteBuying><BanknoteSelling>50.3349</BanknoteSelling>'
    '<CrossRateUSD/><CrossRateOther/></Currency>\n'
    '<Currency CrossOrder="2" Kod="DKK" CurrencyCode="DKK"><Unit>1</Unit>'
    '<Isim>DANİMARKA KRONU</Isim><CurrencyName>DANISH KRONE</CurrencyName>'
    '<ForexBuying>7.4309</ForexBuying><ForexSelling>7.4674</ForexSelling></Currency>\n'
    '<Currency CrossOrder="9" Kod="EUR" CurrencyCode="EUR"><Unit>1</Unit>'
    '<Isim>EURO</Isim><CurrencyName>EURO</CurrencyName>'
    '<ForexBuying>57.9000</ForexBuying><ForexSelling>58.0000</ForexSelling>'
    '<BanknoteBuying>57.8600</BanknoteBuying><BanknoteSelling>58.0900</BanknoteSelling>'
    '</Currency>\n'
    '<Currency CrossOrder="3" Kod="JPY" CurrencyCode="JPY"><Unit>100</Unit>'
    '<Isim>JAPON YENI</Isim><CurrencyName>JAPANESE YEN</CurrencyName>'
    '<ForexBuying>32.0000</ForexBuying><ForexSelling>32.2000</ForexSelling></Currency>\n'
    '<Currency CrossOrder="99" Kod="XDR" CurrencyCode="XDR"><Unit>1</Unit>'
    '<CurrencyName>SPECIAL DRAWING RIGHT (SDR)</CurrencyName>'
    '<ForexBuying>67.1</ForexBuying><ForexSelling/></Currency>\n'
    '</Tarih_Date>\n'
).encode("iso-8859-9")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(fetch=None, today=date(2026, 11, 12), clock=None):
    calls = []

    def fake():
        calls.append(1)
        return SAMPLE if fetch is None else fetch()

    rates = fx.FxRates(fetch=fake, clock=clock or Clock(), today=lambda: today)
    return rates, calls


def test_parses_the_bulletin_with_its_date_units_and_odd_encoding():
    day, rates = fx.parse_tcmb(SAMPLE)
    assert day == date(2026, 11, 12)
    assert rates["USD"]["ForexSelling"] == 50.2595 and rates["USD"]["name"] == "US DOLLAR"
    assert rates["JPY"]["unit"] == 100
    assert rates["DKK"]["name"] == "DANISH KRONE"
    assert "ForexSelling" not in rates["XDR"]


def test_usd_in_try_lists_buying_and_selling_with_the_bulletin_date():
    rates, _ = make()
    out = rates.answer("USD")
    assert out.splitlines() == [
        "FX_RATES status=ok bulletin_date=12.11.2026 today=12.11.2026",
        f"source: {fx.SOURCE}",
        "1 USD (US DOLLAR) in TRY: forex buying 50.1691, forex selling 50.2595, "
        "banknote buying 50.1340, banknote selling 50.3349",
    ]
    assert "not a live market price" in out


def test_an_older_bulletin_is_labelled_with_its_date():
    rates, _ = make(today=date(2026, 11, 13))
    out = rates.answer("USD")
    assert "today=13.11.2026" in out
    assert ("note: this is the newest bulletin, from 12.11.2026; the bulletin for a business "
            "day appears around 15:30 Turkey time") in out
    assert "50.2595" in out  # the value is still given


def test_several_codes_units_unknown_codes_and_cross_rates():
    rates, _ = make()
    out = rates.answer("usd, jpy;CHF")
    assert "100 JPY (JAPANESE YEN) in TRY: forex buying 32.0000" in out
    assert "CHF: not in the TCMB bulletin" in out
    cross = rates.answer(["EUR"], base="USD")
    mid_eur, mid_usd = (57.9 + 58.0) / 2, (50.1691 + 50.2595) / 2
    assert f"1 EUR = {mid_eur / mid_usd:.4f} USD (derived from the TCMB TRY mid rates" in cross


def test_bad_arguments_are_tool_errors():
    rates, calls = make()
    for codes, base in (("", "TRY"), ("DOLLAR", "TRY"), ("USD", "XYZ1")):
        with pytest.raises(ToolError):
            rates.answer(codes, base)
    with pytest.raises(ToolError):
        rates.answer("USD", base="CHF")  # not in the bulletin


def test_network_failure_and_bad_feed_are_failed_results():
    def down():
        raise OSError("timed out")

    rates, _ = make(fetch=down)
    assert rates.answer("USD") == "FX_RATES status=failed reason=network"
    rates, _ = make(fetch=lambda: b"<html>maintenance</html>")
    assert rates.answer("USD") == "FX_RATES status=failed reason=bad_feed"
    rates, _ = make(fetch=lambda: b"")
    assert rates.answer("USD") == "FX_RATES status=failed reason=bad_feed"


def _bulletin(*currencies):
    body = "".join(
        f'<Currency Kod="{code}" CurrencyCode="{code}"><Unit>{unit}</Unit>'
        f"<CurrencyName>{code}</CurrencyName><ForexBuying>{buy}</ForexBuying>"
        f"<ForexSelling>{sell}</ForexSelling></Currency>" for code, unit, buy, sell in currencies)
    return f'<Tarih_Date Tarih="12.11.2026">{body}</Tarih_Date>'.encode()


@pytest.mark.parametrize("unit", ["inf", "-1", "0", "0.5", "nan", "1e400", "2.5", "abc",
                                  "10000000"])
def test_a_currency_with_a_malformed_unit_is_skipped(unit):
    data = _bulletin(("USD", "1", "50", "51"), ("GBP", unit, "60", "61"))
    _, rates = fx.parse_tcmb(data)
    assert set(rates) == {"USD"}
    rates_reader, _ = make(fetch=lambda: data)
    assert "GBP: not in the TCMB bulletin" in rates_reader.answer("USD,GBP")
    with pytest.raises(ToolError):
        rates_reader.answer("USD", base="GBP")


@pytest.mark.parametrize("value", ["inf", "nan", "-3", "0", "1e400", "x"])
def test_malformed_rates_are_dropped(value):
    data = _bulletin(("USD", "1", value, value), ("EUR", "1", "58", "59"))
    _, rates = fx.parse_tcmb(data)
    assert set(rates) == {"EUR"}
    data = _bulletin(("USD", "1", value, value))
    with pytest.raises(fx.FeedError):
        fx.parse_tcmb(data)
    rates_reader, _ = make(fetch=lambda: data)
    assert rates_reader.answer("USD") == "FX_RATES status=failed reason=bad_feed"


def test_cross_rates_with_odd_units_never_raise():
    data = _bulletin(("USD", "1", "50", "51"), ("JPY", "100", "32", "33"))
    rates_reader, _ = make(fetch=lambda: data)
    assert "1 USD = " in rates_reader.answer("USD", base="JPY")


def test_unexpected_errors_become_a_failed_result():
    rates_reader, _ = make()
    rates_reader.bulletin = lambda: (_ for _ in ()).throw(ZeroDivisionError())
    assert rates_reader.answer("USD") == "FX_RATES status=failed reason=internal_error"
    rates_reader.bulletin = lambda: (date(2026, 11, 12), {
        "USD": {"unit": 0, "name": "x", "ForexBuying": 1.0},
        "EUR": {"unit": 1, "name": "y", "ForexBuying": 2.0}})
    assert rates_reader.answer("EUR", base="USD") == "FX_RATES status=failed reason=internal_error"


def test_a_dtd_is_refused():
    evil = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            b'<Tarih_Date Tarih="12.11.2026"></Tarih_Date>')
    with pytest.raises(fx.FeedError):
        fx.parse_tcmb(evil)


def test_the_feed_is_cached_for_the_ttl():
    clock = Clock()
    rates, calls = make(clock=clock)
    rates.answer("USD")
    clock.now = fx.DEFAULT_TTL - 1
    rates.answer("EUR")
    assert len(calls) == 1
    clock.now = fx.DEFAULT_TTL + 1
    rates.answer("USD")
    assert len(calls) == 2


def test_a_failure_is_not_cached():
    state = {"down": True}

    def flaky():
        if state["down"]:
            raise OSError("down")
        return SAMPLE

    rates, calls = make(fetch=flaky)
    assert "failed" in rates.answer("USD")
    state["down"] = False
    assert "status=ok" in rates.answer("USD")
    assert len(calls) == 2


def test_registered_tool_and_its_description():
    registry = ToolRegistry()
    rates, _ = make()
    fx.register_fx_tools(registry, rates)
    assert "doviz_kuru" in registry.names()
    assert "50.2595" in registry.run("doviz_kuru", {"codes": "USD"})
    desc = registry.get("doviz_kuru").description
    assert "before web_ara" in desc and "bulletin date" in desc


def test_setting_turns_it_off():
    assert Settings().fx_rates is True
    assert load_settings(env={"IGRIS_FX_RATES": "0"}).fx_rates is False
    with pytest.raises(ValueError):
        load_settings(env={"IGRIS_FX_RATES": "maybe"})
