"""Versioned stdlib PBKDF2 credentials, with a fresh salt for each password."""
import base64
import hashlib
import hmac
import secrets

ITERATIONS = 600_000
MAX_PASSWORD_BYTES = 1024
MIN_PASSWORD_LENGTH = 12


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("Password must contain at least 12 characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError("Password must not exceed 1024 UTF-8 bytes")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS, base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")
    )


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        algorithm, rounds, salt64, digest64 = encoded.split("$")
        iterations = int(rounds)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        salt = base64.b64decode(salt64, validate=True)
        expected = base64.b64decode(digest64, validate=True)
        if len(salt) < 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, AttributeError):
        return False
