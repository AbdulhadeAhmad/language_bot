"""Typed, compact IDs for application-owned records."""

from datetime import datetime, timezone
import hashlib
import re
import secrets

BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BASE = len(BASE32)
ID_LENGTH = 8
TIMESTAMP_LENGTH = 6
RANDOM_LENGTH = 2
EPOCH = datetime(2015, 5, 15, 9, 14, 40, tzinfo=timezone.utc)

PREFIXES = {
    "sentence": "ST",
    "user": "UR",
    "operation": "OP",
    "audio": "AU",
    "media": "MD",
    "review_session": "RS",
    "review_attempt": "RA",
    "model_job": "MJ",
    "access_change": "AC",
}
PREFIX_KINDS = {prefix: kind for kind, prefix in PREFIXES.items()}
_BODY = re.compile(rf"^[{BASE32}]{{{ID_LENGTH}}}$")
_TOKEN = re.compile(
    rf"(?<![A-Z0-9])#(?P<prefix>{'|'.join(PREFIX_KINDS)})_"
    rf"(?P<body>[{BASE32}]{{{ID_LENGTH}}})(?![A-Z0-9-])",
    re.IGNORECASE,
)


def _encode_base32(value: int, width: int | None = None) -> str:
    if value < 0:
        raise ValueError("Base32 value must be non-negative")
    if value == 0:
        encoded = BASE32[0]
    else:
        chars = []
        while value:
            value, remainder = divmod(value, BASE)
            chars.append(BASE32[remainder])
        encoded = "".join(reversed(chars))
    if width is not None:
        if len(encoded) > width:
            raise OverflowError("Encoded value exceeds the requested width")
        encoded = encoded.rjust(width, BASE32[0])
    return encoded


def decode_base32(value: str) -> int:
    result = 0
    for char in value.upper():
        digit = BASE32.find(char)
        if digit < 0:
            raise ValueError(f"Invalid Base32 character: {char!r}")
        result = result * BASE + digit
    return result


def generate_id(*, now: datetime | None = None) -> str:
    """Return six Base32 timestamp characters followed by two random characters."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("ID timestamp must be timezone-aware")
    seconds = int((now.astimezone(timezone.utc) - EPOCH).total_seconds())
    timestamp = _encode_base32(seconds, TIMESTAMP_LENGTH)
    suffix = "".join(secrets.choice(BASE32) for _ in range(RANDOM_LENGTH))
    return timestamp + suffix


def derive_id(namespace: str, *parts: object) -> str:
    """Derive a stable Base32 ID for idempotent internal model requests."""
    payload = "\0".join((namespace, *(str(part) for part in parts))).encode()
    value = int.from_bytes(hashlib.sha256(payload).digest()[:5], "big")
    return _encode_base32(value, ID_LENGTH)


def format_id(kind: str, body: str) -> str:
    prefix = PREFIXES[kind]
    normalized = body.upper()
    if not _BODY.fullmatch(normalized):
        raise ValueError("Invalid custom ID body")
    return f"#{prefix}_{normalized}"


def normalize_id(value: str, *, expected_kind: str | None = None) -> str:
    """Accept a typed Telegram ID or a raw body used by internal callbacks."""
    value = value.strip().upper()
    if value.startswith("#"):
        match = re.fullmatch(r"#([A-Z]{2})_([0-9A-HJKMNP-TV-Z]{8})", value)
        if not match:
            raise ValueError("Invalid typed ID")
        prefix, body = match.groups()
        kind = PREFIX_KINDS.get(prefix)
        if kind is None or expected_kind is not None and kind != expected_kind:
            raise ValueError("Unexpected ID type")
        return body
    if not _BODY.fullmatch(value):
        raise ValueError("Invalid custom ID body")
    return value


def find_typed_ids(text: str):
    """Return recognized typed IDs in text as (kind, body) pairs."""
    return [
        (PREFIX_KINDS[match.group("prefix").upper()], match.group("body").upper())
        for match in _TOKEN.finditer(text)
    ]


def generate_claim_token() -> str:
    """Return a high-entropy opaque fencing token; it is not a record ID."""
    return "".join(secrets.choice(BASE32) for _ in range(24))
