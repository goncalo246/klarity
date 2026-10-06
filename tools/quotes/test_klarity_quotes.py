"""Testes da tarefa diária de cotações. Sem rede: os fornecedores entram como funções falsas."""
import datetime
import os
import tempfile
import unittest
from decimal import Decimal

import klarity_quotes as kq

HEADER = "key,type,ticker,aliases,exchange,currency,provider,symbol,name\n"
VWCE = "IE00BK5BQT80.XETR,ETF,VWCE,,XETR,EUR,,,Vanguard FTSE All-World (Acc)\n"
IWDA = "IE00B4L5Y983.XAMS,ETF,IWDA,EUNL SWDA,XAMS,EUR,,,iShares Core MSCI World (Acc)\n"
AAPL = "US0378331005.XNAS,STOCK,AAPL,,XNAS,USD,finnhub,AAPL,Apple\n"
MSFT = "US5949181045.XNAS,STOCK,MSFT,,XNAS,USD,finnhub,MSFT,Microsoft\n"
BTC = "bitcoin,CRYPTO,BTC,,,EUR,coingecko,bitcoin,Bitcoin\n"
TODAY = datetime.date(2026, 10, 1)

ECB = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube>
<Cube time="2026-10-01"><Cube currency="USD" rate="1.1298"/><Cube currency="GBP" rate="0.85373"/></Cube>
<Cube time="2026-09-30"><Cube currency="USD" rate="1.1330"/><Cube currency="GBP" rate="0.85100"/></Cube>
<Cube time="2026-09-29"><Cube currency="USD" rate="1.1301"/><Cube currency="GBP" rate="0.85000"/></Cube>
</Cube>
</gesmes:Envelope>"""


def problems(source):
    with unittest.TestCase().assertRaises(kq.CatalogError) as raised:
        kq.parse_catalog_source(source)
    return raised.exception.problems


def quote(date, price):
    return kq.Quote(datetime.date.fromisoformat(date), Decimal(price))


def providers(finnhub=None, coingecko=None):
    """Fornecedores falsos: cada um devolve (cotações por chave, erros por chave) para as entradas que lhe cabem."""
    def fake(result):
        quotes, errors = result if result is not None else ({}, {})
        return lambda entries: (
            {e.key: quotes[e.key] for e in entries if e.key in quotes},
            {e.key: errors.get(e.key, "sem resposta") for e in entries if e.key not in quotes},
        )
    return {"finnhub": fake(finnhub), "coingecko": fake(coingecko)}


class CatalogValidation(unittest.TestCase):

    def test_reads_identity_and_provider_of_each_entry(self):
        entries = kq.parse_catalog_source("# comentário\n" + HEADER + IWDA + AAPL + BTC)
        self.assertEqual(["IE00B4L5Y983.XAMS", "US0378331005.XNAS", "bitcoin"], [e.key for e in entries])
        self.assertEqual(("EUNL", "SWDA"), entries[0].aliases)
        self.assertEqual("", entries[0].provider)
        self.assertEqual(("finnhub", "AAPL", "USD"), (entries[1].provider, entries[1].symbol, entries[1].currency))

    def test_an_entry_quoted_in_pence_does_not_enter_the_catalogue(self):
        found = problems(HEADER + "IE0032077012.XLON,ETF,EQQQ,,XLON,GBp,,,Invesco Nasdaq-100\n")
        self.assertEqual(1, len(found))
        self.assertIn("IE0032077012.XLON", found[0])
        self.assertIn("GBp", found[0])

    def test_an_entry_without_currency_is_rejected_not_assumed_to_be_euros(self):
        found = problems(HEADER + "IE00BK5BQT80.XETR,ETF,VWCE,,XETR,,,,Vanguard FTSE All-World (Acc)\n")
        self.assertIn("moeda", found[0])

    def test_the_same_isin_on_two_exchanges_is_rejected(self):
        found = problems(HEADER + VWCE + "IE00BK5BQT80.XLON,ETF,VWRA,,XLON,USD,,,Vanguard FTSE All-World (Acc)\n")
        self.assertEqual(1, len(found))
        self.assertIn("IE00BK5BQT80", found[0])
        self.assertIn("linha 3", found[0])

    def test_an_isin_with_a_wrong_check_digit_is_rejected(self):
        found = problems(HEADER + "IE00BK5BQT81.XETR,ETF,VWCE,,XETR,EUR,,,Vanguard FTSE All-World (Acc)\n")
        self.assertIn("ISIN", found[0])

    def test_the_key_of_a_security_must_be_its_isin_and_exchange(self):
        found = problems(HEADER + "IE00BK5BQT80.XAMS,ETF,VWCE,,XETR,EUR,,,Vanguard FTSE All-World (Acc)\n")
        self.assertIn("XETR", found[0])

    def test_all_problems_are_reported_at_once_with_their_lines(self):
        found = problems(HEADER + VWCE
                         + "IE0032077012.XLON,ETF,EQQQ,,XLON,GBp,,,Invesco Nasdaq-100\n"
                         + "bitcoin,CRYPTO,BTC,,,EUR,kraken,bitcoin,Bitcoin\n")
        self.assertEqual(2, len(found))
        self.assertIn("linha 3", found[0])
        self.assertIn("kraken", found[1])

    def test_a_comma_inside_a_name_is_caught_as_a_wrong_column_count(self):
        found = problems(HEADER + "US0378331005.XNAS,STOCK,AAPL,,XNAS,USD,finnhub,AAPL,Apple, Inc.\n")
        self.assertIn("colunas", found[0])

    def test_isin_check_digit(self):
        self.assertTrue(kq.isin_is_valid("US0378331005"))
        self.assertTrue(kq.isin_is_valid("IE00BK5BQT80"))
        self.assertFalse(kq.isin_is_valid("US0378331006"))
        self.assertFalse(kq.isin_is_valid("US037833100"))


class PublishedFiles(unittest.TestCase):

    def test_the_published_catalogue_carries_identity_only(self):
        entries = kq.parse_catalog_source(HEADER + IWDA + AAPL)
        self.assertEqual(
            "key,type,ticker,aliases,exchange,currency,name\n"
            "IE00B4L5Y983.XAMS,ETF,IWDA,EUNL SWDA,XAMS,EUR,iShares Core MSCI World (Acc)\n"
            "US0378331005.XNAS,STOCK,AAPL,,XNAS,USD,Apple\n",
            kq.catalog_csv(entries))

    def test_prices_are_written_as_plain_decimals_with_at_most_six_places(self):
        text = kq.quotes_csv({"a": quote("2026-10-01", "142.3850"), "b": quote("2026-10-01", "0.00001234567"),
                              "c": quote("2026-09-30", "75348")})
        self.assertEqual("key,date,price\na,2026-10-01,142.385\nb,2026-10-01,0.000012\nc,2026-09-30,75348\n", text)

    def test_quotes_survive_a_round_trip(self):
        quotes = {"US0378331005.XNAS": quote("2026-10-01", "328.655"), "bitcoin": quote("2026-10-01", "75348")}
        self.assertEqual(quotes, kq.parse_quotes(kq.quotes_csv(quotes)))


class Run(unittest.TestCase):

    def run_job(self, source, previous_quotes="", previous_fx="", finnhub=None, coingecko=None, ecb=ECB):
        def fetch_ecb():
            if ecb is None:
                raise OSError("sem rede")
            return ecb
        return kq.run(source, previous_quotes, previous_fx, providers(finnhub, coingecko), fetch_ecb, TODAY)

    def test_collected_quotes_are_published_under_the_catalogue_key(self):
        result = self.run_job(HEADER + AAPL + BTC,
                              finnhub=({"US0378331005.XNAS": quote("2026-10-01", "328.655")}, {}),
                              coingecko=({"bitcoin": quote("2026-10-01", "75348")}, {}))
        self.assertEqual("key,date,price\nUS0378331005.XNAS,2026-10-01,328.655\nbitcoin,2026-10-01,75348\n", result.quotes)

    def test_a_failed_collection_keeps_the_previous_quote_with_its_own_date(self):
        previous = "key,date,price\nUS0378331005.XNAS,2026-09-29,325.1\nUS5949181045.XNAS,2026-09-29,510\n"
        result = self.run_job(HEADER + AAPL + MSFT, previous_quotes=previous,
                              finnhub=({"US5949181045.XNAS": quote("2026-10-01", "512.4")},
                                       {"US0378331005.XNAS": "HTTP 500"}))
        self.assertIn("US0378331005.XNAS,2026-09-29,325.1\n", result.quotes)
        self.assertIn("US5949181045.XNAS,2026-10-01,512.4\n", result.quotes)
        self.assertIn("AAPL", result.catalog)
        self.assertTrue(any("US0378331005.XNAS" in line and "2026-09-29" in line and "HTTP 500" in line
                            for line in result.report))

    def test_a_failed_collection_with_no_previous_quote_leaves_the_catalogue_intact(self):
        result = self.run_job(HEADER + AAPL, finnhub=({}, {"US0378331005.XNAS": "sem chave"}))
        self.assertEqual("key,date,price\n", result.quotes)
        self.assertIn("US0378331005.XNAS,STOCK,AAPL", result.catalog)
        self.assertTrue(any("US0378331005.XNAS" in line and "sem chave" in line for line in result.report))

    def test_an_entry_without_provider_is_in_the_catalogue_and_reported_as_manual(self):
        result = self.run_job(HEADER + VWCE)
        self.assertIn("IE00BK5BQT80.XETR", result.catalog)
        self.assertEqual("key,date,price\n", result.quotes)
        self.assertTrue(any("IE00BK5BQT80.XETR" in line and "sem fornecedor" in line for line in result.report))

    def test_a_price_of_zero_counts_as_a_failed_collection(self):
        previous = "key,date,price\nbitcoin,2026-09-30,74000\n"
        result = self.run_job(HEADER + BTC, previous_quotes=previous,
                              coingecko=({"bitcoin": quote("2026-10-01", "0")}, {}))
        self.assertIn("bitcoin,2026-09-30,74000\n", result.quotes)

    def test_a_quote_dated_in_the_future_counts_as_a_failed_collection(self):
        result = self.run_job(HEADER + BTC, coingecko=({"bitcoin": quote("2026-10-02", "75000")}, {}))
        self.assertEqual("key,date,price\n", result.quotes)

    def test_an_older_quote_never_replaces_a_newer_one(self):
        previous = "key,date,price\nbitcoin,2026-10-01,75000\n"
        result = self.run_job(HEADER + BTC, previous_quotes=previous,
                              coingecko=({"bitcoin": quote("2026-09-30", "74000")}, {}))
        self.assertIn("bitcoin,2026-10-01,75000\n", result.quotes)

    def test_a_quote_of_an_entry_removed_from_the_catalogue_is_dropped_and_reported(self):
        previous = "key,date,price\nbitcoin,2026-09-30,74000\nUS0378331005.XNAS,2026-09-30,325.1\n"
        result = self.run_job(HEADER + AAPL, previous_quotes=previous,
                              finnhub=({"US0378331005.XNAS": quote("2026-10-01", "328.655")}, {}))
        self.assertNotIn("bitcoin", result.quotes)
        self.assertTrue(any("bitcoin" in line for line in result.report))

    def test_an_invalid_catalogue_refuses_to_publish(self):
        with self.assertRaises(kq.CatalogError):
            self.run_job(HEADER + AAPL + "IE0032077012.XLON,ETF,EQQQ,,XLON,GBp,,,Invesco Nasdaq-100\n")

    def test_exchange_rates_come_from_the_ecb_for_supported_currencies_only(self):
        result = self.run_job(HEADER + AAPL)
        self.assertEqual("currency,date,units_per_eur\n"
                         "USD,2026-10-01,1.1298\nUSD,2026-09-30,1.133\nUSD,2026-09-29,1.1301\n", result.fx)

    def test_an_ecb_failure_keeps_the_previous_rates(self):
        previous = "currency,date,units_per_eur\nUSD,2026-09-30,1.133\n"
        result = self.run_job(HEADER + AAPL, previous_fx=previous, ecb=None)
        self.assertEqual(previous, result.fx)
        self.assertTrue(any("BCE" in line and "sem rede" in line for line in result.report))

    def test_an_ecb_answer_that_declares_entities_is_not_parsed(self):
        previous = "currency,date,units_per_eur\nUSD,2026-09-30,1.133\n"
        hostile = ECB.replace("<gesmes:Envelope", '<!DOCTYPE x [<!ENTITY a "aaaa">]>\n<gesmes:Envelope', 1)
        result = self.run_job(HEADER + AAPL, previous_fx=previous, ecb=hostile)
        self.assertEqual(previous, result.fx)
        self.assertTrue(any("BCE" in line for line in result.report))

    def test_new_rates_are_merged_with_the_previous_ones_and_only_the_latest_days_are_kept(self):
        previous = "currency,date,units_per_eur\n" + "".join(
            "USD,2026-09-%02d,1.1\n" % day for day in range(28, 10, -1))
        result = self.run_job(HEADER + AAPL, previous_fx=previous)
        rows = result.fx.splitlines()[1:]
        self.assertEqual(kq.FX_DAYS, len(rows))
        self.assertEqual("USD,2026-10-01,1.1298", rows[0])
        self.assertEqual("USD,2026-09-28,1.1", rows[3])


class Providers(unittest.TestCase):

    def test_finnhub_quote_takes_the_close_and_the_day_of_its_timestamp(self):
        # 2026-10-01 20:00 UTC, o fecho de Nova Iorque
        payload = '{"c":328.655,"d":-0.36,"dp":-0.11,"h":332.47,"l":328.37,"o":330,"pc":329.02,"t":1790884800}'
        self.assertEqual(quote("2026-10-01", "328.655"), kq.finnhub_quote(payload))

    def test_finnhub_answers_zero_for_a_symbol_it_does_not_know(self):
        with self.assertRaises(kq.CollectError):
            kq.finnhub_quote('{"c":0,"d":null,"dp":null,"h":0,"l":0,"o":0,"pc":0,"t":0}')

    def test_coingecko_prices_in_euros_with_the_day_they_were_updated(self):
        payload = '{"bitcoin":{"eur":75348,"last_updated_at":1790895430},"cardano":{"eur":0.218694,"last_updated_at":1790895430}}'
        quotes, errors = kq.coingecko_quotes(payload, ["bitcoin", "cardano", "nope"])
        self.assertEqual({"bitcoin": quote("2026-10-01", "75348"), "cardano": quote("2026-10-01", "0.218694")}, quotes)
        self.assertEqual(["nope"], list(errors))


class Publishing(unittest.TestCase):

    def test_an_invalid_catalogue_leaves_the_published_folder_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "catalog_source.csv")
            out = os.path.join(folder, "published")
            os.mkdir(out)
            with open(os.path.join(out, "quotes.csv"), "w", encoding="utf-8") as f:
                f.write("key,date,price\nbitcoin,2026-09-30,74000\n")
            with open(source, "w", encoding="utf-8") as f:
                f.write(HEADER + "IE0032077012.XLON,ETF,EQQQ,,XLON,GBp,,,Invesco Nasdaq-100\n")
            code = kq.publish(source, out, providers(), lambda: ECB, TODAY, print_line=lambda line: None)
            self.assertEqual(1, code)
            self.assertEqual(["quotes.csv"], os.listdir(out))
            with open(os.path.join(out, "quotes.csv"), encoding="utf-8") as f:
                self.assertEqual("key,date,price\nbitcoin,2026-09-30,74000\n", f.read())

    def test_the_catalogue_is_also_copied_to_where_the_app_keeps_its_seed(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "catalog_source.csv")
            out = os.path.join(folder, "published")
            seed = os.path.join(folder, "assets", "quote_catalog.csv")
            fixture = os.path.join(folder, "resources", "catalog.csv")
            with open(source, "w", encoding="utf-8") as f:
                f.write(HEADER + AAPL + BTC)
            code = kq.publish(source, out, providers(), lambda: ECB, TODAY, print_line=lambda line: None,
                              catalog_copies=[seed, fixture])
            self.assertEqual(0, code)
            with open(os.path.join(out, "catalog.csv"), encoding="utf-8") as f:
                published = f.read()
            for copy in (seed, fixture):
                with open(copy, encoding="utf-8") as f:
                    self.assertEqual(published, f.read())

    def test_an_invalid_catalogue_does_not_touch_the_seed_either(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "catalog_source.csv")
            seed = os.path.join(folder, "quote_catalog.csv")
            with open(seed, "w", encoding="utf-8") as f:
                f.write("anterior\n")
            with open(source, "w", encoding="utf-8") as f:
                f.write(HEADER + "IE0032077012.XLON,ETF,EQQQ,,XLON,GBp,,,Invesco Nasdaq-100\n")
            code = kq.publish(source, os.path.join(folder, "published"), providers(), lambda: ECB, TODAY,
                              print_line=lambda line: None, catalog_copies=[seed])
            self.assertEqual(1, code)
            with open(seed, encoding="utf-8") as f:
                self.assertEqual("anterior\n", f.read())

    def test_a_valid_run_writes_the_three_files_and_reads_the_previous_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "catalog_source.csv")
            out = os.path.join(folder, "published")
            os.mkdir(out)
            with open(os.path.join(out, "quotes.csv"), "w", encoding="utf-8") as f:
                f.write("key,date,price\nUS0378331005.XNAS,2026-09-29,325.1\n")
            with open(source, "w", encoding="utf-8") as f:
                f.write(HEADER + AAPL + BTC)
            fakes = providers(finnhub=({}, {"US0378331005.XNAS": "HTTP 500"}),
                              coingecko=({"bitcoin": quote("2026-10-01", "75348")}, {}))
            code = kq.publish(source, out, fakes, lambda: ECB, TODAY, print_line=lambda line: None)
            self.assertEqual(0, code)
            self.assertEqual(["catalog.csv", "fx.csv", "quotes.csv"], sorted(os.listdir(out)))
            with open(os.path.join(out, "quotes.csv"), encoding="utf-8") as f:
                self.assertEqual("key,date,price\nUS0378331005.XNAS,2026-09-29,325.1\nbitcoin,2026-10-01,75348\n", f.read())


if __name__ == "__main__":
    unittest.main()
