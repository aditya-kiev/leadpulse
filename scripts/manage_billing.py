"""
Manage LeadPulse subscriptions from the command line, without a payment
processor. Mutates ``organizations.billing_status`` / billing timestamps.

Usage:
    python scripts/manage_billing.py --list-overdue
    python scripts/manage_billing.py --mark-paid <slug-or-uuid> [--customer-id cus_xxx]
    python scripts/manage_billing.py --mark-past-due <slug-or-uuid>
    python scripts/manage_billing.py --suspend <slug-or-uuid>
    python scripts/manage_billing.py --reactivate <slug-or-uuid>

Can be run either way (no PYTHONPATH needed):
    python scripts/manage_billing.py ...
    python -m scripts.manage_billing ...

Requires DATABASE_URL (or .env).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("manage_billing")


async def _lookup(factory, ref: str):
    from app.database.crud import get_organization_by_id, get_organization_by_slug
    from uuid import UUID

    async with factory() as session:
        try:
            org_id = UUID(ref)
        except ValueError:
            org_id = None
        if org_id is not None:
            org = await get_organization_by_id(session, org_id)
        else:
            org = await get_organization_by_slug(session, ref)
        return org


async def run_cmd(args: argparse.Namespace) -> int:
    from app.database.session import async_session_factory
    from app.services.billing import (
        get_organization_by_id_or_slug,
        list_overdue_orgs,
        mark_org_paid,
        mark_org_past_due,
        reactivate_org,
        suspend_org,
    )

    async with async_session_factory() as session:
        if args.list_overdue:
            overdue = await list_overdue_orgs(session)
            if not overdue:
                print("No overdue subscriptions.")
                return 0
            print(f"{len(overdue)} overdue subscription(s):")
            for org in overdue:
                print(f"  - {org.slug}  status={org.billing_status}  "
                      f"next_payment_due_at={org.next_payment_due_at}")
            return 0

        org = await get_organization_by_id_or_slug(session, args.ref)
        if org is None:
            print(f"No organization found for {args.ref!r}.", file=sys.stderr)
            return 1

        if args.mark_paid:
            await mark_org_paid(session, org, provider_customer_id=args.customer_id)
            action = "marked paid"
        elif args.mark_past_due:
            await mark_org_past_due(session, org)
            action = "marked past due"
        elif args.suspend:
            await suspend_org(session, org)
            action = "suspended"
        elif args.reactivate:
            await reactivate_org(session, org)
            action = "reactivated"
        else:
            print("No action specified.", file=sys.stderr)
            return 1

        await session.commit()
        print(f"Organization {org.slug} ({org.id}) {action}: "
              f"billing_status={org.billing_status}, "
              f"last_payment_at={org.last_payment_at}, "
              f"next_payment_due_at={org.next_payment_due_at}")
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage LeadPulse subscriptions (billing_status) without a payment processor.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-overdue", action="store_true",
                       help="List orgs past next_payment_due_at that are not suspended")
    group.add_argument("--mark-paid", dest="mark_paid", action="store_true",
                       help="Mark the org active (status=active, last_payment_at=now, due=+30d)")
    group.add_argument("--mark-past-due", dest="mark_past_due", action="store_true",
                       help="Mark the org past_due")
    group.add_argument("--suspend", action="store_true",
                       help="Suspend the org (widget stops serving)")
    group.add_argument("--reactivate", action="store_true",
                       help="Re-activate the org (status=active, fresh +30d due date)")
    parser.add_argument("ref", nargs="?", help="Organization slug or UUID (for mutation actions)")
    parser.add_argument("--customer-id", help="Stripe customer id to store on the org (with --mark-paid)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.list_overdue and not args.ref:
        print("ref (slug or UUID) is required for mutation actions.", file=sys.stderr)
        return 1
    return asyncio.run(run_cmd(args))


if __name__ == "__main__":
    sys.exit(main())
