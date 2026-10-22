"""Currency minor-unit helpers. Unknown ISO currencies use the common two-digit exponent."""
from decimal import Decimal, ROUND_FLOOR

ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"}
ONE_DECIMAL = {"MGA", "MRU"}
THREE_DECIMAL = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}


def exponent(currency: str) -> int:
    code = currency.upper()
    if code in ZERO_DECIMAL: return 0
    if code in ONE_DECIMAL: return 1
    if code in THREE_DECIMAL: return 3
    return 2


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-exponent(currency))


def floor_money(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(quantum(currency), rounding=ROUND_FLOOR)


def format_money(amount: Decimal, currency: str) -> str:
    return f"{amount:.{exponent(currency)}f}"


def validate_precision(amount: Decimal, currency: str) -> None:
    if amount != amount.quantize(quantum(currency)):
        raise ValueError(f"{currency.upper()} amount exceeds its {exponent(currency)}-digit minor-unit precision")

