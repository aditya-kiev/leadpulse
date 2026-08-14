import base64
import json
import logging
from uuid import UUID

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config.settings import settings

logger = logging.getLogger(__name__)

_SALT = b"lead-agent-crm-encryption-salt-v1"

def check_production_encryption_key() -> None:
    """Raise RuntimeError if CRM_ENCRYPTION_KEY is unset whenever real
    multi-tenant credentials could exist: production, AUTH_ENABLED=true, or
    any onboarding has happened. Never silently degrade to plaintext."""
    needs_key = settings.environment == "production" or settings.auth_enabled
    if needs_key and not settings.crm_encryption_key:
        raise RuntimeError(
            "CRM_ENCRYPTION_KEY must be set in production or when AUTH_ENABLED=true. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )


# Production safeguard: fail at import time if CRM_ENCRYPTION_KEY is unset in production
check_production_encryption_key()


def _derive_key(master_key: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=600_000)
    return base64.urlsafe_b64encode(kdf.derive(master_key.encode()))


def encrypt_json(data: dict, tenant_id: UUID | None = None) -> str:
    master = settings.crm_encryption_key
    if not master:
        raise RuntimeError(
            "CRM_ENCRYPTION_KEY must be configured before encrypting tenant "
            "credentials. Refusing to store credentials as reversible plaintext. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    plain = json.dumps(data, sort_keys=True).encode()
    fernet = Fernet(_derive_key(master))
    token = fernet.encrypt(plain)
    logger.debug("encrypted CRM config tenant=%s len=%d", tenant_id, len(token))
    return token.decode()


def decrypt_json(encrypted: str, tenant_id: UUID | None = None) -> dict:
    master = settings.crm_encryption_key
    if not master:
        raise RuntimeError(
            "CRM_ENCRYPTION_KEY must be configured before decrypting tenant "
            "credentials. Refusing to treat reversible encoding as encryption."
        )
    fernet = Fernet(_derive_key(master))
    plain = fernet.decrypt(encrypted.encode())
    return json.loads(plain.decode())
