"""Tarefa diária das cotações da Klarity.

Lê o catálogo mantido à mão (catalog_source.csv), vai buscar uma cotação por ativo ao fornecedor indicado
em cada linha e a taxa de câmbio ao BCE, e publica três ficheiros que a app descarrega:

    catalog.csv   key,type,ticker,aliases,exchange,currency,name     identidade; muda raramente
    quotes.csv    key,date,price                                     última cotação, na moeda do catálogo
    fx.csv        currency,date,units_per_eur                        taxas de referência do BCE

A chave de um título é ISIN.BOLSA (o mesmo ISIN é cotado em várias bolsas e moedas); a de uma criptomoeda
é o identificador do CoinGecko. Os campos nunca levam vírgulas: a app lê as linhas com um split simples.

Regras:
  - um catálogo inválido não publica nada: a tarefa termina com código 1 e a lista dos problemas;
  - uma recolha falhada mantém a cotação anterior com a data que ela tinha; o catálogo não muda;
  - a chave do Finnhub vem da variável FINNHUB_KEY ou de finnhub.key no local.properties do projeto.

Uso:  python klarity_quotes.py [--source catalog_source.csv] [--out published] [--copy-catalog FICHEIRO ...]

Quando o catálogo muda, a semente que vai dentro da app e a base dos testes têm de mudar com ele; passar
--copy-catalog duas vezes, com ../../app/src/main/assets/quote_catalog.csv e
../../app/src/test/resources/quotes/catalog.csv.

Se o Python não reconhecer o certificado do BCE (acontece em Windows), aponta SSL_CERT_FILE para um
conjunto de autoridades de certificação, por exemplo o que vem com o Git; a verificação nunca se desliga.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections import namedtuple
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

SUPPORTED_CURRENCIES = ("EUR", "USD")
TYPES = ("ETF", "STOCK", "CRYPTO")
PROVIDERS = ("", "finnhub", "coingecko")
FX_DAYS = 10
PRICE_PLACES = Decimal("0.000001")

SOURCE_HEADER = "key,type,ticker,aliases,exchange,currency,provider,symbol,name"
CATALOG_HEADER = "key,type,ticker,aliases,exchange,currency,name"
QUOTES_HEADER = "key,date,price"
FX_HEADER = "currency,date,units_per_eur"

Entry = namedtuple("Entry", "key type ticker aliases exchange currency provider symbol name")
Quote = namedtuple("Quote", "date price")
Rate = namedtuple("Rate", "currency date units_per_eur")
Result = namedtuple("Result", "catalog quotes fx report")


class CatalogError(Exception):
    """O catálogo de origem tem linhas inválidas; nada é publicado."""

    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


class CollectError(Exception):
    """O fornecedor não deu uma cotação utilizável para um ativo."""


# ---------------------------------------------------------------------------------------------- catálogo

def isin_is_valid(isin):
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
        return False
    digits = "".join(str(int(char, 36)) for char in isin)
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = int(char)
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _data_lines(text, header):
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#") and line.lower() != header:
            yield number, line


def parse_catalog_source(text):
    """Devolve as entradas do catálogo ou lança CatalogError com todos os problemas, um por linha inválida."""
    entries = []
    problems = []
    keys = {}
    isins = {}
    for number, line in _data_lines(text, SOURCE_HEADER):
        columns = [column.strip() for column in line.split(",")]
        if len(columns) != 9:
            problems.append("linha %d: esperadas 9 colunas, encontradas %d (um nome não pode ter vírgulas)"
                            % (number, len(columns)))
            continue
        key, kind, ticker, aliases, exchange, currency, provider, symbol, name = columns
        problem = _entry_problem(key, kind, ticker, exchange, currency, provider, symbol, name)
        if problem is None and key in keys:
            problem = "chave repetida (já está na linha %d)" % keys[key]
        isin = key.split(".")[0] if kind != "CRYPTO" else None
        if problem is None and isin in isins:
            problem = "o ISIN %s já está na linha %d; só entra uma listagem por ISIN" % (isin, isins[isin])
        if problem is not None:
            problems.append("linha %d: %s: %s" % (number, key or "(sem chave)", problem))
            continue
        keys[key] = number
        if isin is not None:
            isins[isin] = number
        entries.append(Entry(key, kind, ticker, tuple(aliases.split()), exchange, currency, provider, symbol, name))
    if problems:
        raise CatalogError(problems)
    return entries


def _entry_problem(key, kind, ticker, exchange, currency, provider, symbol, name):
    if kind not in TYPES:
        return "tipo '%s' desconhecido (aceites: %s)" % (kind, ", ".join(TYPES))
    if not currency:
        return "falta a moeda; nunca se assume euros"
    if currency not in SUPPORTED_CURRENCIES:
        return "moeda '%s' não suportada (aceites: %s)" % (currency, ", ".join(SUPPORTED_CURRENCIES))
    if not ticker or not name:
        return "falta o ticker ou o nome"
    if provider not in PROVIDERS:
        return "fornecedor '%s' desconhecido" % provider
    if provider and not symbol:
        return "falta o símbolo no fornecedor %s" % provider
    if kind == "CRYPTO":
        if exchange or not re.fullmatch(r"[a-z0-9-]+", key):
            return "uma criptomoeda tem por chave o identificador do CoinGecko e não tem bolsa"
        return None
    if not re.fullmatch(r"[A-Z0-9]{4}", exchange):
        return "a bolsa '%s' não é um código MIC de quatro caracteres" % exchange
    isin = key.split(".")[0]
    if not isin_is_valid(isin):
        return "o ISIN '%s' não é válido (dígito de controlo)" % isin
    if key != isin + "." + exchange:
        return "a chave tem de ser ISIN.BOLSA, aqui %s.%s" % (isin, exchange)
    return None


def catalog_csv(entries):
    lines = [CATALOG_HEADER]
    for e in entries:
        lines.append(",".join((e.key, e.type, e.ticker, " ".join(e.aliases), e.exchange, e.currency, e.name)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------- cotações

def _plain(number):
    """Decimal sem expoente e sem zeros à direita, com seis casas no máximo."""
    text = format(number.quantize(PRICE_PLACES, rounding=ROUND_HALF_EVEN), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def quotes_csv(quotes):
    lines = [QUOTES_HEADER]
    for key, q in quotes.items():
        lines.append("%s,%s,%s" % (key, q.date.isoformat(), _plain(q.price)))
    return "\n".join(lines) + "\n"


def parse_quotes(text):
    quotes = {}
    for _, line in _data_lines(text, QUOTES_HEADER):
        key, date, price = line.split(",")
        quotes[key] = Quote(datetime.date.fromisoformat(date), Decimal(price))
    return quotes


def _merge_quotes(entries, previous, collected, errors, today):
    quotes = {}
    report = []
    for e in entries:
        new = collected.get(e.key)
        reason = errors.get(e.key)
        if not e.provider:
            reason = "sem fornecedor configurado (cotação manual na app)"
        elif new is not None and new.price <= 0:
            new, reason = None, "preço %s" % _plain(new.price)
        elif new is not None and new.date > today:
            new, reason = None, "data no futuro (%s)" % new.date.isoformat()
        old = previous.get(e.key)
        if new is not None and (old is None or new.date >= old.date):
            quotes[e.key] = new
        elif old is not None:
            quotes[e.key] = old
            report.append("%s (%s): mantida a cotação de %s: %s"
                          % (e.key, e.ticker, old.date.isoformat(), reason or "a recolhida é mais antiga"))
        else:
            report.append("%s (%s): sem cotação: %s" % (e.key, e.ticker, reason or "sem resposta"))
    known = {e.key for e in entries}
    for key in previous:
        if key not in known:
            report.append("%s: saiu do catálogo; a cotação deixa de ser publicada" % key)
    return quotes, report


# ----------------------------------------------------------------------------------------------- câmbio

def parse_ecb(xml):
    """Taxas de referência do BCE (unidades da moeda por 1 euro) para as moedas suportadas."""
    # O ficheiro do BCE não declara DTD; um que o faça não é o do BCE e não chega ao analisador de XML.
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ValueError("resposta com declarações de entidades")
    rates = []
    for day in ElementTree.fromstring(xml).iter("{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}Cube"):
        if "time" not in day.attrib:
            continue
        date = datetime.date.fromisoformat(day.attrib["time"])
        for cube in day:
            currency = cube.attrib.get("currency")
            if currency in SUPPORTED_CURRENCIES and currency != "EUR":
                rate = Decimal(cube.attrib["rate"])
                if rate > 0:
                    rates.append(Rate(currency, date, rate))
    return rates


def parse_fx(text):
    rates = []
    for _, line in _data_lines(text, FX_HEADER):
        currency, date, rate = line.split(",")
        rates.append(Rate(currency, datetime.date.fromisoformat(date), Decimal(rate)))
    return rates


def fx_csv(rates):
    lines = [FX_HEADER]
    for r in rates:
        lines.append("%s,%s,%s" % (r.currency, r.date.isoformat(), _plain(r.units_per_eur)))
    return "\n".join(lines) + "\n"


def _merge_fx(previous, new):
    """Junta as taxas novas às anteriores e fica com os FX_DAYS dias mais recentes de cada moeda."""
    by_day = {(r.currency, r.date): r for r in previous}
    by_day.update({(r.currency, r.date): r for r in new})
    merged = []
    for currency in sorted({r.currency for r in by_day.values()}):
        days = sorted((r for r in by_day.values() if r.currency == currency), key=lambda r: r.date, reverse=True)
        merged.extend(days[:FX_DAYS])
    return merged


# ---------------------------------------------------------------------------------------------- a tarefa

def run(source_text, previous_quotes_text, previous_fx_text, providers, fetch_ecb, today):
    """O conteúdo dos três ficheiros e o relatório. Não toca em disco nem em rede: os fornecedores vêm de fora.

    providers: nome -> função(entradas) que devolve (cotações por chave, erros por chave).
    """
    entries = parse_catalog_source(source_text)
    collected = {}
    errors = {}
    for name in PROVIDERS:
        mine = [e for e in entries if e.provider == name]
        if name and mine:
            quotes, failed = providers[name](mine)
            collected.update(quotes)
            errors.update(failed)
    quotes, report = _merge_quotes(entries, parse_quotes(previous_quotes_text), collected, errors, today)

    previous_rates = parse_fx(previous_fx_text)
    try:
        rates = _merge_fx(previous_rates, parse_ecb(fetch_ecb()))
    except (OSError, ElementTree.ParseError, InvalidOperation, ValueError) as e:
        rates = previous_rates
        report.append("BCE: mantidas as taxas anteriores: %s" % e)
    return Result(catalog_csv(entries), quotes_csv(quotes), fx_csv(rates), report)


def publish(source_path, out_dir, providers, fetch_ecb, today, print_line=print, catalog_copies=()):
    """Corre a tarefa e escreve os três ficheiros em out_dir. Devolve o código de saída.

    catalog_copies: outros sítios onde o catálogo publicado tem de estar igual (a semente dentro da app e a
    base dos testes), para as cópias nunca divergirem.
    """
    with open(source_path, encoding="utf-8") as f:
        source = f.read()
    try:
        result = run(source, _read(os.path.join(out_dir, "quotes.csv")), _read(os.path.join(out_dir, "fx.csv")),
                     providers, fetch_ecb, today)
    except CatalogError as e:
        print_line("Catálogo inválido; nada foi publicado:")
        for problem in e.problems:
            print_line("  " + problem)
        return 1
    os.makedirs(out_dir, exist_ok=True)
    files = {"catalog.csv": result.catalog, "quotes.csv": result.quotes, "fx.csv": result.fx}
    for name, content in files.items():
        with open(os.path.join(out_dir, name + ".tmp"), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    for name in files:
        os.replace(os.path.join(out_dir, name + ".tmp"), os.path.join(out_dir, name))
    for copy in catalog_copies:
        os.makedirs(os.path.dirname(os.path.abspath(copy)), exist_ok=True)
        with open(copy, "w", encoding="utf-8", newline="\n") as f:
            f.write(result.catalog)
    print_line("Publicado em %s: %d ativos no catálogo, %d cotações, %d taxas de câmbio."
               % (out_dir, result.catalog.count("\n") - 1, result.quotes.count("\n") - 1, result.fx.count("\n") - 1))
    for line in result.report:
        print_line("  " + line)
    return 0


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------------------------------ fornecedores

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
FINNHUB_URL = "https://finnhub.io/api/v1/quote?symbol="
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?vs_currencies=eur&include_last_updated_at=true&ids="
FINNHUB_PAUSE_SECONDS = 1.1  # o plano gratuito aceita 60 chamadas por minuto


def _http_get(url, headers=None):
    request = urllib.request.Request(url, headers=dict({"User-Agent": "klarity-quotes/1"}, **(headers or {})))
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _utc_date(timestamp):
    return datetime.datetime.fromtimestamp(int(timestamp), datetime.timezone.utc).date()


def finnhub_quote(payload):
    """A cotação de uma resposta de /quote: o fecho e o dia do seu carimbo temporal."""
    data = json.loads(payload, parse_float=Decimal, parse_int=Decimal)
    close = data.get("c")
    if not close or not data.get("t"):
        raise CollectError("símbolo desconhecido no Finnhub")
    return Quote(_utc_date(data["t"]), Decimal(close))


def finnhub_provider(api_key, get=_http_get, pause=time.sleep):
    def collect(entries):
        quotes, errors = {}, {}
        for index, e in enumerate(entries):
            if not api_key:
                errors[e.key] = "sem chave do Finnhub"
                continue
            if index:
                pause(FINNHUB_PAUSE_SECONDS)
            try:
                quotes[e.key] = finnhub_quote(get(FINNHUB_URL + urllib.parse.quote(e.symbol),
                                                  {"X-Finnhub-Token": api_key}))
            except (OSError, ValueError, CollectError) as error:
                errors[e.key] = _reason(error)
        return quotes, errors
    return collect


def coingecko_quotes(payload, ids):
    data = json.loads(payload, parse_float=Decimal, parse_int=Decimal)
    quotes, errors = {}, {}
    for coin in ids:
        item = data.get(coin) or {}
        if item.get("eur") is None or not item.get("last_updated_at"):
            errors[coin] = "identificador desconhecido no CoinGecko"
        else:
            quotes[coin] = Quote(_utc_date(item["last_updated_at"]), Decimal(item["eur"]))
    return quotes, errors


def coingecko_provider(get=_http_get):
    def collect(entries):
        by_symbol = {e.symbol: e.key for e in entries}
        try:
            quotes, errors = coingecko_quotes(get(COINGECKO_URL + ",".join(by_symbol)), list(by_symbol))
        except (OSError, ValueError) as error:
            return {}, {e.key: _reason(error) for e in entries}
        return ({by_symbol[s]: q for s, q in quotes.items()}, {by_symbol[s]: r for s, r in errors.items()})
    return collect


def _reason(error):
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP %d" % error.code
    return str(error) or error.__class__.__name__


def finnhub_key(project_root):
    key = os.environ.get("FINNHUB_KEY", "").strip()
    properties = os.path.join(project_root, "local.properties")
    if not key and os.path.exists(properties):
        with open(properties, encoding="utf-8") as f:
            for line in f:
                name, _, value = line.partition("=")
                if name.strip() == "finnhub.key":
                    key = value.strip()
    return key


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Gera catalog.csv, quotes.csv e fx.csv da Klarity.")
    parser.add_argument("--source", default=os.path.join(here, "catalog_source.csv"))
    parser.add_argument("--out", default=os.path.join(here, "published"))
    parser.add_argument("--copy-catalog", action="append", default=[], metavar="FICHEIRO",
                        help="copia também o catálogo publicado para este ficheiro; pode repetir-se")
    args = parser.parse_args(argv)
    if not sys.stdout.isatty():
        # Redirecionado para um ficheiro ou para o registo de uma tarefa agendada: o relatório sai em UTF-8.
        sys.stdout.reconfigure(encoding="utf-8")
    providers = {
        "finnhub": finnhub_provider(finnhub_key(os.path.dirname(os.path.dirname(here)))),
        "coingecko": coingecko_provider(),
    }
    return publish(args.source, args.out, providers, lambda: _http_get(ECB_URL), datetime.date.today(),
                   catalog_copies=args.copy_catalog)


if __name__ == "__main__":
    sys.exit(main())
