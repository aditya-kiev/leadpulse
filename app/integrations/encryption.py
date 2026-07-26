import base64
import logging
from uuid import UUID

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config.settings import settings

logger = logging.getLogger(__name__)

_SALT = b"lead-agent-crm-encryption-salt-v1"


def _derive_key(master_key: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=600_000)
    return base64.urlsafe_b64encode(kdf.derive(master_key.encode()))


def encrypt_json(data: dict, tenant_id: UUID | None = None) -> str:
    master = settings.crm_encryption_key
    if not master:
        logger.warning("CRM_ENCRYPTION_KEY not set — storing credentials in plaintext")
        return base64.urlsafe_b64encode(str(data).encode()).decode()
    fernet = Fernet(_derive_key(master))
    plain = str(data).encode()
    token = fernet.encrypt(plain)
    logger.debug("encrypted CRM config tenant=%s len=%d", tenant_id, len(token))
    return token.decode()


def decrypt_json(encrypted: str, tenant_id: UUID | None = None) -> dict:
    master = settings.crm_encryption_key
    if not master:
        raw = base64.urlsafe_b64decode(encrypted.encode())
        return eval(raw.decode())
    fernet = Fernet(_derive_key(master))
    plain = fernet.decrypt(encrypted.encode())
    return eval(plain.decode())
