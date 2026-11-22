"""COST-01/SEC-10: exact, bounded USD values; no implicit float conversion."""

import re
from decimal import Decimal, localcontext
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer, StrictInt, WithJsonSchema
from pydantic import Field as PydanticField

MAX_USD = Decimal("1000000000")
USD_PATTERN = r"^(0|[1-9][0-9]{0,9})(\.[0-9]{1,18})?$"


def parse_usd(value: object) -> Decimal:
    if isinstance(value, str):
        if not re.fullmatch(USD_PATTERN, value):
            raise ValueError("USD must be a nonnegative decimal string with at most 18 places")
        result = Decimal(value)
    elif isinstance(value, Decimal):
        result = value
    else:
        raise ValueError("USD requires a decimal string or Decimal; floats are forbidden")
    if (
        not result.is_finite()
        or result.is_signed()
        or result > MAX_USD
        or result.as_tuple().exponent < -18
    ):
        raise ValueError("USD is non-finite, negative, over precision, or above the limit")
    return result


def usd_string(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def add_usd(*values: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return parse_usd(sum((parse_usd(value) for value in values), Decimal("0")))


Usd = Annotated[
    Decimal,
    BeforeValidator(parse_usd),
    PlainSerializer(usd_string, return_type=str),
    WithJsonSchema({"type": "string", "pattern": USD_PATTERN}),
]
TokenCount = Annotated[StrictInt, PydanticField(ge=0)]
