"""
Bank Statement Parser — handles CSV, OFX/QBO, and PDF bank statements.

Detects bank statement formats and extracts individual transactions.
Each transaction becomes a signal.financial.transaction with observables
for merchant names, account numbers, and amounts.

Supported formats:
- CSV exports (Chase, Bank of America, Wells Fargo, generic)
- OFX/QBO (Open Financial Exchange — QuickBooks, Quicken, Mint)
- PDF statements (text extraction → AI-assisted line parsing)
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime
from typing import Optional
from xml.etree import ElementTree

from storage.ingest.parsers import ParseResult, register_parser
from storage.ingest.parsers.base import BaseParser
from storage.signals.models import Observable


# ─── Column Name Detection ────────────────────────────────────────────────────
# Maps common bank CSV header names to canonical field names.
# Banks export with wildly different column names — this normalizes them.

_DATE_COLUMNS = {
    "date", "posting date", "post date", "transaction date", "trans date",
    "posted date", "effective date", "value date", "trade date", "settlement date",
    "booking date",
}

_AMOUNT_COLUMNS = {
    "amount", "transaction amount", "debit/credit", "net amount", "value",
    "sum", "total",
}

_DEBIT_COLUMNS = {
    "debit", "debits", "withdrawal", "withdrawals", "debit amount",
    "money out", "charge",
}

_CREDIT_COLUMNS = {
    "credit", "credits", "deposit", "deposits", "credit amount",
    "money in", "payment",
}

_DESCRIPTION_COLUMNS = {
    "description", "memo", "narrative", "details", "transaction description",
    "payee", "name", "merchant", "merchant name", "transaction details",
    "reference", "particulars",
}

_BALANCE_COLUMNS = {
    "balance", "running balance", "available balance", "closing balance",
    "ledger balance",
}

_TYPE_COLUMNS = {
    "type", "transaction type", "trans type", "category", "tran type",
}

_ACCOUNT_COLUMNS = {
    "account", "account number", "account no", "acct", "card number",
    "card no", "account name",
}


def _normalize_header(h: str) -> str:
    """Lowercase, strip whitespace and quotes from header name."""
    return h.strip().strip('"').strip("'").lower()


def _map_headers(headers: list[str]) -> dict[str, str]:
    """Map raw CSV headers to canonical field names."""
    mapping = {}
    for raw in headers:
        norm = _normalize_header(raw)
        if norm in _DATE_COLUMNS:
            mapping[raw] = "date"
        elif norm in _AMOUNT_COLUMNS:
            mapping[raw] = "amount"
        elif norm in _DEBIT_COLUMNS:
            mapping[raw] = "debit"
        elif norm in _CREDIT_COLUMNS:
            mapping[raw] = "credit"
        elif norm in _DESCRIPTION_COLUMNS:
            mapping[raw] = "description"
        elif norm in _BALANCE_COLUMNS:
            mapping[raw] = "balance"
        elif norm in _TYPE_COLUMNS:
            mapping[raw] = "type"
        elif norm in _ACCOUNT_COLUMNS:
            mapping[raw] = "account"
    return mapping


# ─── Amount Parsing ───────────────────────────────────────────────────────────

_AMOUNT_RE = re.compile(r'^[($\s]*(-?\$?\s*[\d,]+\.?\d*)\s*\)?$')


def _parse_amount(value: str) -> Optional[float]:
    """Parse a dollar amount string into a float."""
    if not value or not value.strip():
        return None
    val = value.strip()

    # Handle parenthetical negatives: (123.45) → -123.45
    is_negative = val.startswith("(") and val.endswith(")")
    if is_negative:
        val = val[1:-1]

    # Remove $ and commas
    val = val.replace("$", "").replace(",", "").strip()

    try:
        result = float(val)
        return -result if is_negative else result
    except ValueError:
        return None


# ─── Date Parsing ─────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%m/%d/%Y",      # 01/15/2026
    "%m/%d/%y",      # 01/15/26
    "%Y-%m-%d",      # 2026-01-15
    "%m-%d-%Y",      # 01-15-2026
    "%d/%m/%Y",      # 15/01/2026 (EU)
    "%b %d, %Y",     # Jan 15, 2026
    "%B %d, %Y",     # January 15, 2026
    "%d %b %Y",      # 15 Jan 2026
    "%Y%m%d",        # 20260115
]


def _parse_date(value: str) -> Optional[str]:
    """Parse a date string into ISO format."""
    if not value or not value.strip():
        return None
    val = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(val, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ─── OFX/QBO Detection ───────────────────────────────────────────────────────

_OFX_MARKERS = [
    b"<OFX>", b"<ofx>",
    b"OFXHEADER:", b"ofxheader:",
    b"<BANKMSGSRSV1>", b"<CREDITCARDMSGSRSV1>",
    b"<STMTTRN>",
]


def _is_ofx(raw_bytes: bytes) -> bool:
    """Check if bytes look like OFX/QBO format."""
    head = raw_bytes[:2000].lower()
    return any(marker.lower() in head for marker in _OFX_MARKERS)


def _parse_ofx(raw_bytes: bytes) -> tuple[list[dict], dict]:
    """
    Parse OFX/QBO file into transactions.

    OFX is SGML-like (not strict XML). We handle both old-style SGML
    and newer XML-based OFX 2.x.
    """
    text = raw_bytes.decode("utf-8", errors="replace")
    transactions = []
    account_info = {}

    # Extract account ID
    acct_match = re.search(r'<ACCTID>([^<\n]+)', text, re.IGNORECASE)
    if acct_match:
        account_info["account_number"] = acct_match.group(1).strip()

    bank_match = re.search(r'<BANKID>([^<\n]+)', text, re.IGNORECASE)
    if bank_match:
        account_info["routing_number"] = bank_match.group(1).strip()

    acct_type = re.search(r'<ACCTTYPE>([^<\n]+)', text, re.IGNORECASE)
    if acct_type:
        account_info["account_type"] = acct_type.group(1).strip()

    org_match = re.search(r'<ORG>([^<\n]+)', text, re.IGNORECASE)
    if org_match:
        account_info["institution"] = org_match.group(1).strip()

    # Extract balance
    bal_match = re.search(r'<BALAMT>([^<\n]+)', text, re.IGNORECASE)
    if bal_match:
        account_info["balance"] = _parse_amount(bal_match.group(1))

    bal_date = re.search(r'<DTASOF>([^<\n]+)', text, re.IGNORECASE)
    if bal_date:
        account_info["balance_date"] = _parse_ofx_date(bal_date.group(1).strip())

    # Extract transactions (STMTTRN blocks)
    trn_pattern = re.compile(
        r'<STMTTRN>(.*?)</STMTTRN>',
        re.IGNORECASE | re.DOTALL,
    )

    # Some OFX files don't close STMTTRN — handle unclosed tags
    if not trn_pattern.search(text):
        trn_pattern = re.compile(
            r'<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|</STMTRS>|$)',
            re.IGNORECASE | re.DOTALL,
        )

    for match in trn_pattern.finditer(text):
        block = match.group(1)
        txn = _parse_ofx_transaction(block)
        if txn:
            transactions.append(txn)

    return transactions, account_info


def _parse_ofx_date(value: str) -> Optional[str]:
    """Parse OFX date format (YYYYMMDDHHMMSS or YYYYMMDD)."""
    val = value.strip()[:8]  # Take just YYYYMMDD
    if len(val) >= 8 and val[:8].isdigit():
        try:
            dt = datetime.strptime(val[:8], "%Y%m%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_ofx_transaction(block: str) -> Optional[dict]:
    """Parse a single OFX STMTTRN block into a transaction dict."""
    def _extract(tag: str) -> Optional[str]:
        m = re.search(rf'<{tag}>([^<\n]+)', block, re.IGNORECASE)
        return m.group(1).strip() if m else None

    amount_str = _extract("TRNAMT")
    if not amount_str:
        return None

    amount = _parse_amount(amount_str)
    if amount is None:
        return None

    date_str = _extract("DTPOSTED") or _extract("DTUSER")
    date = _parse_ofx_date(date_str) if date_str else None

    return {
        "date": date,
        "amount": amount,
        "description": _extract("NAME") or _extract("MEMO") or "",
        "memo": _extract("MEMO") or "",
        "type": _extract("TRNTYPE") or "OTHER",
        "transaction_id": _extract("FITID") or "",
        "check_number": _extract("CHECKNUM"),
        "ref_number": _extract("REFNUM"),
        "sic": _extract("SIC"),
    }


# ─── CSV Bank Statement Detection ────────────────────────────────────────────

def _is_bank_csv(headers: list[str]) -> float:
    """Score how likely these CSV headers are a bank statement."""
    norms = {_normalize_header(h) for h in headers}

    has_date = bool(norms & _DATE_COLUMNS)
    has_amount = bool(norms & _AMOUNT_COLUMNS) or bool(norms & _DEBIT_COLUMNS) or bool(norms & _CREDIT_COLUMNS)
    has_desc = bool(norms & _DESCRIPTION_COLUMNS)

    if has_date and has_amount and has_desc:
        return 0.92  # Very likely bank statement
    if has_date and has_amount:
        return 0.80  # Probable bank statement
    if has_date and has_desc:
        return 0.60  # Maybe — could be other tabular data
    return 0.0


def _parse_bank_csv(raw_bytes: bytes) -> tuple[list[dict], dict]:
    """Parse a bank CSV export into normalized transactions."""
    text = raw_bytes.decode("utf-8", errors="replace")

    # Some bank CSVs have header rows before the actual data
    # (e.g., "Account Number: ****1234\nDate,Description,Amount")
    lines = text.strip().split("\n")
    account_info = {}
    data_start = 0

    for i, line in enumerate(lines[:10]):  # Check first 10 lines for preamble
        stripped = line.strip()
        if not stripped:
            data_start = i + 1
            continue
        # Detect preamble lines (key: value or key,value with no other commas)
        if ":" in stripped and stripped.count(",") <= 1:
            k, v = stripped.split(":", 1) if ":" in stripped else (stripped, "")
            k = k.strip().lower()
            v = v.strip().strip('"')
            if "account" in k:
                account_info["account_number"] = v
            elif "date" in k and "range" in k:
                account_info["date_range"] = v
            data_start = i + 1
            continue
        # If this line has multiple commas/tabs, it's probably the header row
        if stripped.count(",") >= 2 or stripped.count("\t") >= 2:
            data_start = i
            break

    csv_text = "\n".join(lines[data_start:])
    if not csv_text.strip():
        return [], account_info

    try:
        dialect = csv.Sniffer().sniff(csv_text[:4000])
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(csv_text), dialect=dialect)
    if not reader.fieldnames:
        return [], account_info

    header_map = _map_headers(list(reader.fieldnames))
    transactions = []

    for i, row in enumerate(reader):
        if i >= 50000:  # Safety cap
            break

        # Build canonical transaction from mapped headers
        txn = {}
        for raw_col, canonical in header_map.items():
            val = row.get(raw_col, "").strip()
            if val:
                txn[canonical] = val

        # Must have at least a date or amount to be a valid transaction
        if not txn.get("date") and not txn.get("amount") and not txn.get("debit") and not txn.get("credit"):
            continue

        # Normalize date
        if txn.get("date"):
            parsed_date = _parse_date(txn["date"])
            if parsed_date:
                txn["date"] = parsed_date

        # Normalize amount — handle split debit/credit columns
        if "amount" in txn:
            txn["amount"] = _parse_amount(txn["amount"])
        elif "debit" in txn or "credit" in txn:
            debit = _parse_amount(txn.pop("debit", "")) or 0.0
            credit = _parse_amount(txn.pop("credit", "")) or 0.0
            # Debits are negative (money out), credits positive (money in)
            txn["amount"] = credit - debit if credit else -abs(debit)

        # Normalize balance
        if "balance" in txn:
            txn["balance"] = _parse_amount(txn["balance"])

        # Include unmapped columns as extra fields
        for raw_col in row:
            if raw_col not in header_map and row[raw_col].strip():
                txn[_normalize_header(raw_col)] = row[raw_col].strip()

        transactions.append(txn)

    return transactions, account_info


# ─── PDF Statement Detection ─────────────────────────────────────────────────

# Patterns that indicate a PDF is a bank statement
_STATEMENT_INDICATORS = [
    r"statement\s+(period|date|summary)",
    r"account\s+(number|summary|activity)",
    r"(opening|closing|beginning|ending)\s+balance",
    r"(deposits?|withdrawals?|credits?|debits?)",
    r"transaction\s+(history|detail|date)",
    r"(checking|savings|credit\s+card)\s+(account|statement)",
    r"previous\s+balance",
    r"new\s+balance",
]

# Transaction line pattern in PDF text
# Matches lines like: 01/15  SHELL OIL 04523  -47.82
#                  or: 01/15/2026  AMAZON.COM  $123.45  $4,567.89
_TXN_LINE_RE = re.compile(
    r'(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)'  # date
    r'\s+'
    r'(.+?)'                                       # description
    r'\s+'
    r'(-?\$?[\d,]+\.\d{2})'                       # amount
    r'(?:\s+(-?\$?[\d,]+\.\d{2}))?'               # optional balance
)


def _score_pdf_as_statement(text: str) -> float:
    """Score how likely PDF text is a bank statement."""
    if not text:
        return 0.0

    text_lower = text[:5000].lower()
    hits = sum(1 for pat in _STATEMENT_INDICATORS if re.search(pat, text_lower))

    if hits >= 4:
        return 0.92
    if hits >= 2:
        return 0.75
    if hits >= 1:
        return 0.50
    return 0.0


def _parse_pdf_statement(text: str) -> tuple[list[dict], dict]:
    """Extract transactions from PDF statement text."""
    transactions = []
    account_info = {}

    # Try to extract account number
    acct_match = re.search(
        r'account\s*(?:number|no\.?|#)?\s*:?\s*[*xX]*(\d{4,})',
        text[:3000],
        re.IGNORECASE,
    )
    if acct_match:
        account_info["account_number"] = acct_match.group(1)

    # Try to extract statement period
    period_match = re.search(
        r'(?:statement\s+period|period)\s*:?\s*'
        r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*(?:to|through|-|–)\s*'
        r'(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})',
        text[:3000],
        re.IGNORECASE,
    )
    if period_match:
        start = _parse_date(period_match.group(1))
        end = _parse_date(period_match.group(2))
        if start:
            account_info["period_start"] = start
        if end:
            account_info["period_end"] = end

    # Extract transaction lines
    for match in _TXN_LINE_RE.finditer(text):
        date_str = match.group(1)
        description = match.group(2).strip()
        amount_str = match.group(3)
        balance_str = match.group(4)

        # Skip header-like lines
        if any(kw in description.lower() for kw in ["date", "description", "amount", "balance"]):
            continue

        txn = {
            "date": _parse_date(date_str) or date_str,
            "description": description,
            "amount": _parse_amount(amount_str),
        }
        if balance_str:
            txn["balance"] = _parse_amount(balance_str)

        if txn["amount"] is not None:
            transactions.append(txn)

    return transactions, account_info


# ─── Observable Extraction ────────────────────────────────────────────────────

def _extract_financial_observables(
    transactions: list[dict],
    account_info: dict,
) -> list[Observable]:
    """Extract observables from parsed financial data."""
    observables = []
    seen = set()

    def _add(obs_type: str, value: str, source: str):
        norm = value.strip().lower()
        key = (obs_type, norm)
        if key not in seen and len(norm) >= 2:
            seen.add(key)
            observables.append(Observable(
                observable_type=obs_type,
                observable_value=value.strip(),
                normalized_value=norm,
                source_field=source,
            ))

    # Account-level observables
    if account_info.get("account_number"):
        # Mask all but last 4 digits for storage
        acct = account_info["account_number"]
        masked = f"****{acct[-4:]}" if len(acct) > 4 else acct
        _add("account_number", masked, "account_info.account_number")

    if account_info.get("routing_number"):
        _add("routing_number", account_info["routing_number"], "account_info.routing_number")

    if account_info.get("institution"):
        _add("organization_name", account_info["institution"], "account_info.institution")

    # Transaction-level observables (merchants, check numbers)
    merchants_seen = set()
    for i, txn in enumerate(transactions[:500]):  # Cap for performance
        desc = txn.get("description", "")
        if desc and desc.lower() not in merchants_seen:
            merchants_seen.add(desc.lower())
            _add("merchant_name", desc, f"transactions[{i}].description")

        if txn.get("check_number"):
            _add("check_number", txn["check_number"], f"transactions[{i}].check_number")

    return observables[:200]  # Cap total observables


# ─── Main Parser ─────────────────────────────────────────────────────────────

@register_parser
class BankStatementParser(BaseParser):
    """
    Detects and parses bank statements in CSV, OFX/QBO, and PDF formats.

    Priority is set higher than generic CSV/PDF parsers when bank-specific
    patterns are detected (financial column names, OFX markers, statement
    keywords in PDF text).
    """

    name = "bank_statement"
    format_id = "bank_statement"

    def can_parse(self, raw_bytes: bytes, content_type: Optional[str] = None) -> float:
        # ── OFX/QBO: highest confidence ──────────────────────────────────
        if content_type and ("ofx" in content_type.lower() or "qbo" in content_type.lower()):
            return 0.96

        if _is_ofx(raw_bytes):
            return 0.96

        # ── CSV: check for bank-specific column names ────────────────────
        if content_type and "csv" in content_type.lower():
            try:
                text = raw_bytes[:4000].decode("utf-8", errors="strict")
                # Skip preamble lines to find header row
                for line in text.strip().split("\n")[:10]:
                    if line.count(",") >= 2 or line.count("\t") >= 2:
                        try:
                            dialect = csv.Sniffer().sniff(line)
                            headers = next(csv.reader(io.StringIO(line), dialect))
                            score = _is_bank_csv(headers)
                            if score > 0:
                                return score
                        except csv.Error:
                            pass
                        break
            except (UnicodeDecodeError, ValueError):
                pass
            return 0.0

        # ── Try CSV detection without content-type hint ──────────────────
        try:
            text = raw_bytes[:4000].decode("utf-8", errors="strict")
            lines = text.strip().split("\n")
            if len(lines) >= 2:
                for line in lines[:10]:
                    if line.count(",") >= 2 or line.count("\t") >= 2:
                        try:
                            dialect = csv.Sniffer().sniff(line)
                            headers = next(csv.reader(io.StringIO(line), dialect))
                            score = _is_bank_csv(headers)
                            if score > 0:
                                return score
                        except csv.Error:
                            pass
                        break
        except (UnicodeDecodeError, ValueError):
            pass

        # ── PDF: check for statement indicators in text ──────────────────
        if raw_bytes[:5] == b'%PDF-':
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                    # Read first 3 pages for detection
                    text = ""
                    for page in pdf.pages[:3]:
                        text += (page.extract_text() or "") + "\n"
                    score = _score_pdf_as_statement(text)
                    if score > 0:
                        return score
            except Exception:
                pass

        return 0.0

    def parse(self, raw_bytes: bytes) -> ParseResult:
        file_hash = hashlib.sha256(raw_bytes).hexdigest()

        # ── Route to format-specific parser ──────────────────────────────
        if _is_ofx(raw_bytes):
            return self._parse_ofx_statement(raw_bytes, file_hash)

        if raw_bytes[:5] == b'%PDF-':
            return self._parse_pdf_statement(raw_bytes, file_hash)

        # Default: CSV
        return self._parse_csv_statement(raw_bytes, file_hash)

    def _parse_csv_statement(self, raw_bytes: bytes, file_hash: str) -> ParseResult:
        transactions, account_info = _parse_bank_csv(raw_bytes)
        observables = _extract_financial_observables(transactions, account_info)

        # Compute summary stats
        total_debits = sum(t["amount"] for t in transactions if isinstance(t.get("amount"), (int, float)) and t["amount"] < 0)
        total_credits = sum(t["amount"] for t in transactions if isinstance(t.get("amount"), (int, float)) and t["amount"] > 0)

        parsed = {
            "format": "csv",
            "transaction_count": len(transactions),
            "transactions": transactions,
            "account": account_info,
            "summary": {
                "total_debits": round(total_debits, 2),
                "total_credits": round(total_credits, 2),
                "net": round(total_credits + total_debits, 2),
                "transaction_count": len(transactions),
            },
            "file_sha256": file_hash,
        }

        return ParseResult(
            format_detected="bank_statement/csv",
            confidence=0.90,
            parsed=parsed,
            metadata={
                "parser": "bank_statement",
                "sub_format": "csv",
                "transaction_count": len(transactions),
                "file_sha256": file_hash,
            },
            observables=observables,
            signal_type_hint="signal.financial.transaction",
            parser_name=self.name,
        )

    def _parse_ofx_statement(self, raw_bytes: bytes, file_hash: str) -> ParseResult:
        transactions, account_info = _parse_ofx(raw_bytes)
        observables = _extract_financial_observables(transactions, account_info)

        total_debits = sum(t["amount"] for t in transactions if isinstance(t.get("amount"), (int, float)) and t["amount"] < 0)
        total_credits = sum(t["amount"] for t in transactions if isinstance(t.get("amount"), (int, float)) and t["amount"] > 0)

        parsed = {
            "format": "ofx",
            "transaction_count": len(transactions),
            "transactions": transactions,
            "account": account_info,
            "summary": {
                "total_debits": round(total_debits, 2),
                "total_credits": round(total_credits, 2),
                "net": round(total_credits + total_debits, 2),
                "transaction_count": len(transactions),
            },
            "file_sha256": file_hash,
        }

        return ParseResult(
            format_detected="bank_statement/ofx",
            confidence=0.95,
            parsed=parsed,
            metadata={
                "parser": "bank_statement",
                "sub_format": "ofx",
                "transaction_count": len(transactions),
                "institution": account_info.get("institution", ""),
                "file_sha256": file_hash,
            },
            observables=observables,
            signal_type_hint="signal.financial.transaction",
            parser_name=self.name,
        )

    def _parse_pdf_statement(self, raw_bytes: bytes, file_hash: str) -> ParseResult:
        try:
            import pdfplumber

            full_text = ""
            page_count = 0
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                page_count = len(pdf.pages)
                for page in pdf.pages[:50]:  # Cap at 50 pages
                    full_text += (page.extract_text() or "") + "\n"

            transactions, account_info = _parse_pdf_statement(full_text)
            observables = _extract_financial_observables(transactions, account_info)

            total_debits = sum(t["amount"] for t in transactions if isinstance(t.get("amount"), (int, float)) and t["amount"] < 0)
            total_credits = sum(t["amount"] for t in transactions if isinstance(t.get("amount"), (int, float)) and t["amount"] > 0)

            parsed = {
                "format": "pdf",
                "transaction_count": len(transactions),
                "transactions": transactions,
                "account": account_info,
                "summary": {
                    "total_debits": round(total_debits, 2),
                    "total_credits": round(total_credits, 2),
                    "net": round(total_credits + total_debits, 2),
                    "transaction_count": len(transactions),
                },
                "page_count": page_count,
                "text_preview": full_text[:2000],
                "file_sha256": file_hash,
            }

            return ParseResult(
                format_detected="bank_statement/pdf",
                confidence=0.85,
                parsed=parsed,
                metadata={
                    "parser": "bank_statement",
                    "sub_format": "pdf",
                    "page_count": page_count,
                    "transaction_count": len(transactions),
                    "file_sha256": file_hash,
                },
                observables=observables,
                signal_type_hint="signal.financial.transaction",
                parser_name=self.name,
            )

        except Exception as e:
            return ParseResult(
                format_detected="bank_statement/pdf",
                confidence=0.50,
                parsed={
                    "format": "pdf",
                    "parse_error": str(e),
                    "file_sha256": file_hash,
                },
                metadata={"parser": "bank_statement", "parse_error": str(e)},
                observables=[],
                signal_type_hint="signal.financial.transaction",
                parser_name=self.name,
            )
