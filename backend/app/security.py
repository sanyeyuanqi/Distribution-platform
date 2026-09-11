"""Secret handling. Configuration keys must live outside PostgreSQL and backups."""
import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 256
PASSWORD_TOO_SHORT = f'至少需要 {PASSWORD_MIN_LENGTH} 位'
PASSWORD_TOO_LONG = f'最多允许 {PASSWORD_MAX_LENGTH} 位'


def _cipher() -> Fernet:
    if not settings.encryption_key:
        raise RuntimeError('ENCRYPTION_KEY must be configured')
    return Fernet(settings.encryption_key.encode())


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError('Encrypted credential cannot be read with configured key') from exc


def fingerprint(value: str) -> str:
    if len(settings.secret_key) < 32:
        raise RuntimeError('SECRET_KEY must contain at least 32 characters')
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def mask(value: str) -> str:
    if len(value) < 12:
        return '••••'
    return f'{value[:3]}••••{value[-4:]}'


def validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(PASSWORD_TOO_SHORT)
    if len(value) > PASSWORD_MAX_LENGTH:
        raise ValueError(PASSWORD_TOO_LONG)
    return value


def hash_password(value: str) -> str:
    validate_password(value)
    return _passwords.hash(value)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return len(plain) <= PASSWORD_MAX_LENGTH and _passwords.verify(hashed, plain)
    except (VerificationError, InvalidHashError):
        return False
