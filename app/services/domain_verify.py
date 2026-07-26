"""Domain ownership verification via DNS TXT records.

Verification flow:
1. Org admin sets a custom_domain via PUT /org/branding.
2. A unique verification token is generated and stored in
   Organization.domain_verification_token.
3. Org admin adds a TXT record: _leadpulse-verify.{domain} = {token}
4. Org admin calls POST /org/branding/verify-domain.
5. The service looks up the TXT record and flips
   custom_domain_status to "verified" if the token matches.
"""

import secrets

import dns.resolver

from app.database.models import Organization

TXT_PREFIX = "_leadpulse-verify"


def generate_verification_token() -> str:
    """Generate a cryptographically random 32-char hex token."""
    return secrets.token_hex(16)


def expected_txt_record_name(domain: str) -> str:
    """Return the DNS TXT record name to look up.
    e.g. _leadpulse-verify.leads.brokerage.com
    """
    return f"{TXT_PREFIX}.{domain}"


async def verify_domain_txt(domain: str, expected_token: str) -> bool:
    """Resolve TXT records for the verification subdomain and check token.

    Returns True if a matching token is found. Raises on network errors.
    """
    record_name = expected_txt_record_name(domain)

    try:
        answers = dns.resolver.resolve(record_name, "TXT", lifetime=10)
    except Exception:
        return False

    for rdata in answers:
        txt_value = "".join(s.decode() if isinstance(s, bytes) else s for s in rdata.strings)
        if txt_value.strip().strip('"') == expected_token:
            return True

    return False


def build_verification_instructions(domain: str, token: str) -> str:
    """Return human-readable DNS setup instructions."""
    record = expected_txt_record_name(domain)
    return (
        f"Add a TXT record to your DNS configuration:\n\n"
        f"  Name:  {record}\n"
        f"  Type:  TXT\n"
        f"  Value: {token}\n\n"
        f"Once added, call POST /org/branding/verify-domain to complete verification.\n"
        f"It may take a few minutes for DNS changes to propagate."
    )
