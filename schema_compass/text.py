"""Identifier normalization, abbreviation expansion and datatype families."""
from __future__ import annotations

import re

# Physical-name abbreviations commonly found in warehouse naming standards.
# Expansion is additive: the original token is kept so exact physical matches still score.
ABBREVIATIONS = {
    "acct": "account", "actv": "active", "addr": "address", "amt": "amount", "avg": "average",
    "bal": "balance", "brnd": "brand", "carr": "carrier", "cat": "category", "cd": "code",
    "ccy": "currency", "chnl": "channel", "cnt": "count", "cntry": "country", "crt": "created",
    "ctry": "country", "curr": "currency", "cust": "customer", "desc": "description",
    "disc": "discount", "dlvr": "delivered", "dob": "birth date", "dt": "date", "dvc": "device",
    "eff": "effective", "eml": "email", "emp": "employee", "exp": "expiration", "flg": "flag",
    "fmt": "format", "frst": "first", "fst": "first", "grp": "group", "hnd": "hand",
    "hdr": "header", "id": "identifier", "ind": "indicator", "inv": "inventory", "lgcy": "legacy",
    "ln": "line", "loc": "location", "lst": "last", "lylty": "loyalty", "mktg": "marketing",
    "mo": "month", "mstr": "master", "mth": "month", "nbr": "number", "nm": "name",
    "num": "number", "opn": "opened", "ord": "order", "pct": "percent", "pg": "page",
    "phn": "phone", "pmt": "payment", "prc": "price", "prod": "product", "promo": "promotion",
    "pstl": "postal", "qty": "quantity", "rfnd": "refund", "rgn": "region", "rsn": "reason",
    "rsrv": "reserved", "rtn": "return", "sess": "session", "shp": "shipped", "shpmt": "shipment",
    "snap": "snapshot", "sq": "square", "ft": "feet", "stat": "status", "str": "store",
    "strt": "start", "subcat": "subcategory", "sts": "status", "supp": "supplier",
    "tot": "total", "trkg": "tracking", "ts": "timestamp", "txn": "transaction",
    "typ": "type", "uom": "unit of measure",
    # clinical
    "abn": "abnormal", "appt": "appointment", "attnd": "attending", "demog": "demographics", "disch": "discharge",
    "dx": "diagnosis", "enc": "encounter", "encntr": "encounter", "fac": "facility", "lang": "language",
    "mrn": "medical record number", "npi": "national provider identifier", "prim": "primary", "prov": "provider",
    "pt": "patient", "rslt": "result", "seq": "sequence", "spclty": "specialty",
    # people and HR
    "abs": "absence", "hrs": "hours", "lvl": "level", "mgr": "manager", "orig": "original", "pref": "preferred",
    "req": "requisition", "term": "termination", "ttl": "title",
    # product analytics
    "anon": "anonymous", "csat": "customer satisfaction", "evt": "event", "subj": "subject", "upd": "updated", "vndr": "vendor",
    "vw": "view", "whse": "warehouse", "wk": "week", "yr": "year",
}

# Business synonyms appended as extra tokens so lexical similarity can bridge vocabularies.
SYNONYMS = {
    "customer": ("client", "shopper", "buyer"), "client": ("customer",), "shopper": ("customer",),
    "product": ("item", "sku"), "item": ("product",), "sku": ("product", "item"),
    "identifier": ("key", "number"), "number": ("identifier",), "key": ("identifier",),
    "flag": ("indicator", "is"), "indicator": ("flag",), "is": ("flag",),
    "created": ("signup", "enrolled", "registered"), "signup": ("created", "registration"),
    "status": ("state",), "amount": ("value",), "units": ("quantity",), "quantity": ("units",),
    "description": ("name", "text"), "postal": ("zip",), "zip": ("postal",),
    "region": ("area", "province"), "province": ("region",), "store": ("location", "site"), "shipped": ("ship",),
    "employee": ("worker", "staff"), "worker": ("employee",), "leave": ("absence",), "absence": ("leave",),
    "experiment": ("flag", "variant"), "solved": ("resolved",), "resolved": ("solved",),
}

STOPWORDS = {"the", "a", "an", "of", "for", "and", "or", "to"}

# Semantic role of a field, inferred from its trailing name token(s). Roles let the matcher
# tell an identifier from a name or an amount even when the entity words overlap.
ROLE_TOKENS = {
    "identifier": "identifier", "key": "identifier", "number": "identifier", "sku": "identifier",
    "name": "name", "title": "name",
    "description": "text", "text": "text", "notes": "text", "comment": "text",
    "date": "date", "timestamp": "timestamp", "at": "timestamp", "time": "timestamp",
    "amount": "amount", "price": "amount", "total": "amount", "cost": "amount", "value": "amount",
    "quantity": "quantity", "units": "quantity", "count": "quantity", "feet": "quantity",
    "footage": "quantity", "score": "measure", "rating": "measure", "percent": "measure",
    "code": "code", "type": "code", "status": "code", "tier": "code", "reason": "code",
    "category": "code", "subcategory": "code", "region": "code", "format": "code",
    "flag": "flag", "indicator": "flag", "active": "flag", "in": "flag",
}
_ROLE_COMPATIBLE = {frozenset(p) for p in [("name", "text"), ("identifier", "code"), ("date", "timestamp"),
                                           ("code", "name"), ("amount", "measure"), ("quantity", "measure"),
                                           ("code", "flag")]}


def stem(token: str) -> str:
    """Very light plural folding: orders->order, addresses->address, categories->category."""
    if len(token) <= 3 or token.endswith("ss") or token.endswith("us"):
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("sses", "xes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


def role(name: str | None) -> str | None:
    tokens = expand(split_identifier(name), synonyms=False)
    if tokens and tokens[0] == "is":
        return "flag"
    for token in reversed(tokens[-2:]):
        if token in ROLE_TOKENS:
            return ROLE_TOKENS[token]
    return None


def role_compatibility(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.9
    if a == b:
        return 1.0
    return 0.85 if frozenset((a, b)) in _ROLE_COMPATIBLE else 0.6


def split_identifier(value: str | None) -> list[str]:
    """Split snake_case, camelCase, dotted and spaced identifiers into lowercase tokens."""
    if not value:
        return []
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [t for t in re.findall(r"[a-z0-9]+", value.lower()) if t]


def expand(tokens: list[str], synonyms: bool = True) -> list[str]:
    out: list[str] = []
    for token in tokens:
        long = ABBREVIATIONS.get(token)
        out.extend(long.split() if long else [stem(token)])
    if synonyms:
        out.extend(s for token in list(out) for s in SYNONYMS.get(token, ()))
    return [t for t in out if t not in STOPWORDS]


def normalize(value: str | None, synonyms: bool = True) -> str:
    return " ".join(expand(split_identifier(value), synonyms))


def short_name(physical: str) -> str:
    return physical.rsplit(".", 1)[-1]


def type_family(value: str | None) -> str | None:
    if not value:
        return None
    base = value.upper().split("(")[0].strip()
    if base in {"STRING", "TEXT", "CHAR", "VARCHAR", "CHARACTER", "NVARCHAR", "NCHAR"}:
        return "TEXT"
    if base in {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "LONG", "SHORT", "BYTE"}:
        return "INTEGER"
    if base in {"NUMBER", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL", "FLOAT64"}:
        # NUMBER(p,0) behaves like an integer in Snowflake.
        scale = re.search(r",\s*(\d+)\s*\)", value)
        if base in {"NUMBER", "DECIMAL", "NUMERIC"} and (scale is None or scale.group(1) == "0") \
                and "(" in value:
            return "INTEGER"
        return "DECIMAL"
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return "TIMESTAMP"
    if base == "DATE":
        return "DATE"
    if base in {"BOOLEAN", "BOOL"}:
        return "BOOLEAN"
    if base in {"VARIANT", "OBJECT", "STRUCT", "MAP", "JSON"}:
        return "SEMI_STRUCTURED"
    if base in {"ARRAY"}:
        return "ARRAY"
    return base


# Pairs that load without loss or with a routine cast.
_COMPATIBLE = {
    ("INTEGER", "DECIMAL"), ("DATE", "TIMESTAMP"), ("TIMESTAMP", "DATE"),
    ("INTEGER", "TEXT"), ("BOOLEAN", "TEXT"), ("TEXT", "BOOLEAN"), ("INTEGER", "BOOLEAN"),
    ("DECIMAL", "INTEGER"),
}


def type_compatibility(source: str | None, target: str | None) -> tuple[float, str | None]:
    """Return (score in [0,1], issue text or None) for loading source into target."""
    a, b = type_family(source), type_family(target)
    if not a or not b:
        return 0.6, "Datatype metadata is incomplete."
    if a == b:
        return 1.0, None
    if (a, b) in _COMPATIBLE:
        notes = {
            ("DECIMAL", "INTEGER"): "Decimal to integer may truncate values.",
            ("TIMESTAMP", "DATE"): "Timestamp to date drops time and depends on time zone.",
            ("TEXT", "BOOLEAN"): "Text to boolean needs an explicit value mapping (e.g. 'Y'/'N').",
            ("INTEGER", "BOOLEAN"): "Integer to boolean needs an explicit 0/1 mapping.",
        }
        return 0.7, notes.get((a, b), f"Cast {a.lower()} to {b.lower()}.")
    return 0.0, f"Datatype families differ ({a.lower()} vs {b.lower()})."
