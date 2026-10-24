"""A SYNTHETIC Trade Republic transaction export (SPEC §19.16): the real export's 23 columns and quoting (every field
quoted, comma, dot decimals, not chronological), but every ISIN, name, amount, id, IBAN and person is INVENTED — no
row of a real export is in the repository. One row of every supported type plus one unsupported type."""
import csv
import io

COLUMNS = ("datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol", "shares", "price",
           "amount", "fee", "tax", "currency", "original_amount", "original_currency", "fx_rate", "description",
           "transaction_id", "counterparty_name", "counterparty_iban", "payment_reference", "mcc_code")

# invented private data that must NEVER reach the database or an API response
SECRETS = ("Erika", "Musterfrau", "Max Mustermann", "Mustermann", "DE89370400440532013000", "DE89 3704 0044",
           "GB33BUKB20201555555555", "GB33 BUKB", "REF-SECRET-42", "Bäckerei Geheim", "5462", "6011",
           "DE44500105175407324931", "Einzahlung von", "Card payment")

STOCK, FUND, BOND = "DE000SYN0001", "IE00SYNFUND1", "DE000SYNBND1"
WRT1, WRT2, PRIV = "DE000SYNWRT1", "DE000SYNWRT2", "LU00SYNPRV18"


def tid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def row(dt, cat, typ, n, *, ac="", name="", sym="", shares="", price="", amount="", fee="", tax="", cur="EUR",
        oa="", oc="", fx="", desc="", cp_name="", cp_iban="", ref="", mcc="", day=None):
    return {"datetime": dt, "date": day or dt[:10], "account_type": "DEFAULT", "category": cat, "type": typ,
            "asset_class": ac, "name": name, "symbol": sym, "shares": shares, "price": price, "amount": amount,
            "fee": fee, "tax": tax, "currency": cur, "original_amount": oa, "original_currency": oc, "fx_rate": fx,
            "description": desc, "transaction_id": tid(n), "counterparty_name": cp_name, "counterparty_iban": cp_iban,
            "payment_reference": ref, "mcc_code": mcc}


def to_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in COLUMNS])
    return buf.getvalue().encode("utf-8")


ROWS = [  # deliberately NOT in time order (as the real export); 3 and 6 fractional digits
    row("2025-03-10T10:00:00.000Z", "TRADING", "SELL", 6, ac="STOCK", name="Synthetic AG", sym=STOCK, shares="-12",
        price="130", amount="1560.00", fee="-1.00", tax="-10.50", desc="Sell trade DE000SYN0001 Synthetic AG"),
    row("2025-01-02T08:00:00.123456Z", "CASH", "CUSTOMER_INPAYMENT", 1, amount="5000.00", cp_name="Erika Musterfrau",
        cp_iban="DE89370400440532013000", ref="REF-SECRET-42", mcc="6011",
        desc="Einzahlung von DE89 3704 0044 0532 0130 00 Erika Musterfrau"),
    row("2025-01-03T09:00:00.000Z", "CASH", "TRANSFER_INSTANT_INBOUND", 2, amount="1000.00",
        name="Max Mustermann", cp_name="Max Mustermann", cp_iban="GB33BUKB20201555555555",
        desc="Incoming transfer from Max Mustermann (GB33BUKB20201555555555)"),
    row("2025-12-20T12:00:00.000Z", "CASH", "TRANSFER_INSTANT_OUTBOUND", 3, amount="-300.00",
        cp_name="Erika Musterfrau", cp_iban="DE89370400440532013000",
        desc="Outgoing transfer for Erika Musterfrau GB33 BUKB 2020 1555 5555 55"),
    row("2025-01-10T10:00:00.000Z", "TRADING", "BUY", 4, ac="STOCK", name="Synthetic AG", sym=STOCK, shares="10",
        price="100", amount="-1000.00", fee="-1.00", desc="Buy trade DE000SYN0001 Synthetic AG"),
    row("2025-02-10T10:00:00.000Z", "TRADING", "BUY", 5, ac="STOCK", name="Synthetic AG", sym=STOCK, shares="5",
        price="120", amount="-600.00", fee="-1.00"),
    row("2025-01-15T10:00:00.000Z", "TRADING", "BUY", 7, ac="FUND", name="World Synthetic ETF", sym=FUND,
        shares="4", price="50", amount="-200.00", fee="-1.00"),
    row("2025-04-01T10:00:00.000Z", "TRADING", "SELL", 8, ac="FUND", name="World Synthetic ETF", sym=FUND,
        shares="-2", price="60", amount="120.00", fee="-1.00", tax="2.00"),
    row("2025-02-01T10:00:00.000Z", "TRADING", "BUY", 9, ac="BOND", name="Synthetic Bond 2030", sym=BOND,
        shares="1000", price="0.98", amount="-982.00", fee="-1.00"),          # amount incl. accrued interest
    row("2025-01-20T10:00:00.000Z", "TRADING", "BUY", 10, ac="CRYPTO", name="Synthcoin", sym="SYC", shares="0.5",
        price="20000", amount="-10000.00", fee="-1.00", desc="Buy trade XF000SYC0019 Synthcoin"),
    row("2025-02-20T00:00:00.000Z", "DELIVERY", "FREE_RECEIPT", 11, ac="CRYPTO", name="Synthcoin", sym="SYC",
        shares="0.01", price="21000"),
    row("2025-06-01T10:00:00.000Z", "TRADING", "SELL", 12, ac="CRYPTO", name="Synthcoin", sym="SYC", shares="-0.2",
        price="25000", amount="5000.00", fee="-1.00", desc="Sell trade XF000SYC0019 Synthcoin"),
    row("2025-03-01T10:00:00.000Z", "TRADING", "BUY", 13, ac="DERIVATIVE", name="Synthetic Call", sym=WRT1,
        shares="100", price="1.5", amount="-150.00", fee="-1.00"),
    row("2025-03-15T10:00:00.000Z", "TRADING", "SELL", 14, ac="DERIVATIVE", name="Synthetic Call", sym=WRT1,
        shares="-40", price="2", amount="80.00", fee="-1.00"),
    row("2025-06-20T09:00:00.123Z", "CASH", "TILG", 16, ac="DERIVATIVE", name="Synthetic Call", sym=WRT1,
        amount="30.00"),
    row("2025-06-20T09:00:00.123456Z", "CORPORATE_ACTION", "WARRANT_EXERCISE", 15, ac="DERIVATIVE",
        name="Synthetic Call", sym=WRT1, shares="-60", cur=""),
    row("2025-04-01T11:00:00.000Z", "TRADING", "BUY", 17, ac="DERIVATIVE", name="Synthetic Put", sym=WRT2,
        shares="10", price="1", amount="-10.00", fee="0.00"),
    row("2025-08-01T09:00:00.000Z", "CORPORATE_ACTION", "WARRANT_EXERCISE", 18, ac="DERIVATIVE",
        name="Synthetic Put", sym=WRT2, shares="-10", cur=""),                 # its TILG is missing
    row("2025-05-05T07:00:00.000Z", "CASH", "DIVIDEND", 19, ac="STOCK", name="Synthetic AG", sym=STOCK,
        shares="3", amount="8.40", tax="-2.10", oa="9.00", oc="USD", fx="0.9333"),
    row("2025-07-01T00:00:00.000Z", "CASH", "INTEREST_PAYMENT", 20, name="Interest", amount="12.34", tax="-3.08"),
    row("2025-12-31T00:00:00.000Z", "CASH", "TAX_OPTIMIZATION", 21, amount="0.00", tax="5.55"),
    row("2025-09-01T10:00:00.000Z", "CASH", "PRIVATE_MARKET_BUY", 22, ac="PRIVATE_FUND",
        name="Synthetic Private Equity (pay to DE44500105175407324931)", sym=PRIV, amount="-500.00", fee="0.00"),
    row("2025-10-10T10:00:00.000Z", "CASH", "CARD_TRANSACTION", 23, amount="-12.50", cp_name="Bäckerei Geheim",
        mcc="5462", desc="Card payment Bäckerei Geheim"),
]
CSV = to_csv(ROWS)
