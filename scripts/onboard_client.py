"""
Onboard a new LeadPulse client without touching code.

Creates a new tenant using the existing multi-tenant tables:
  - ``organizations``  — the tenant/agency row
  - ``crm_configs``    — per-tenant Gemini credentials (encrypted at rest)
  - ``users``          — optional org_admin dashboard user

Then generates an embeddable chat widget snippet and prints a setup checklist.
Goal: a new client live in under 15 minutes.

Usage:
    python scripts/onboard_client.py \
        --agency "Bella Vista Realty" \
        --vertical real_estate \
        --gemini-key AIzaSy... \
        [--admin-email owner@bellavista.com] \
        [--notification-phone +15550123] \
        [--slug bellavista] \
        [--brand-name "Bella Vista Realty"] \
        [--primary-color "#9B6B43"] \
        [--logo-url https://www.bellavista.com/logo.png] \
        [--plan-tier growth] \
        [--widget-out bellavista-widget.html] \
        [--api-base https://lead-agent-api.example.com] \
        [--app-hostname app.leadpulse.ai]

Can be run either way (no PYTHONPATH needed):
    python scripts/onboard_client.py ...
    python -m scripts.onboard_client ...

Requires DATABASE_URL (or .env) and, in production, CRM_ENCRYPTION_KEY.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
import uuid
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("onboard_client")

VALID_VERTICALS = ("generic", "real_estate", "insurance")

DEFAULT_WIDGET_TITLE = "Chat with us"


def slugify(name: str) -> str:
    """Lowercase, alphanumeric + hyphen slug for tenant subdomains.

    Shared implementation lives in app/services/slugs.py so the register flow
    and the onboarding CLI can never disagree on slug rules.
    """
    from app.services.slugs import slugify as _slugify

    return _slugify(name)


async def _slug_taken(factory, slug: str) -> bool:
    from app.database.models import Organization

    async with factory() as session:
        result = await session.execute(select(Organization.id).where(Organization.slug == slug))
        return result.scalar_one_or_none() is not None


async def _unique_slug(factory, base: str) -> str:
    candidate = base
    counter = 2
    while await _slug_taken(factory, candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _widget_snippet(
    *,
    tenant_slug: str,
    api_base: str,
    brand_name: str,
    primary_color: str,
    title: str,
    widget_key: str = "",
) -> str:
    """Return a self-contained embeddable chat widget (single <script>).

    The widget authenticates with a per-session demo token (short-lived,
    per-message) alongside the organization's long-lived tenant-bound
    ``X-Widget-Key`` header, which scopes every request to the right tenant.
    """
    css_color = primary_color or "#4F46E5"
    brand = brand_name or "Chat with us"
    widget_key_js = repr(widget_key) if widget_key else "''"
    return f"""<!-- LeadPulse chat widget — tenant: {tenant_slug} -->
<script>
(function () {{
  var API = {api_base!r};
  var BRAND = {brand!r};
  var COLOR = {css_color!r};
  var TITLE = {title!r};
  var WIDGET_KEY = {widget_key_js};
  var sessionId = null, token = null, authHeaders = null, open = false, sending = false;
  var BACKEND_ERROR = 'Sorry, something went wrong on our end — a team member will follow up with you shortly.';

  function css() {{
    return 'position:fixed;right:20px;bottom:20px;z-index:999999;font-family:Inter,system-ui,sans-serif;' +
      'box-sizing:border-box;';
  }}
  function btn() {{
    var el = document.createElement('button');
    el.id = 'lp-launcher';
    el.textContent = '💬 ' + TITLE;
    el.style.cssText = css() +
      'width:auto;height:52px;padding:0 22px;border:none;border-radius:26px;background:' + COLOR +
      ';color:#fff;font-size:15px;font-weight:600;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.18);';
    el.onclick = toggle;
    return el;
  }}
  function panel() {{
    var el = document.createElement('div');
    el.id = 'lp-panel';
    el.style.cssText = css() +
      'width:360px;max-width:calc(100vw - 40px);height:480px;background:#fff;border-radius:16px;' +
      'overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.2);display:none;flex-direction:column;';
    el.innerHTML =
      '<div style="padding:16px 18px;background:' + COLOR + ';color:#fff;font-weight:600;' +
      'font-size:15px;">' + BRAND + '</div>' +
      '<div id="lp-thread" style="flex:1;overflow-y:auto;padding:16px;background:#F7F7FA;' +
      'display:flex;flex-direction:column;gap:10px;"></div>' +
      '<div style="display:flex;gap:8px;padding:12px;border-top:1px solid #E5E7EB;">' +
      '<input id="lp-input" type="text" placeholder="Type your message…" style="flex:1;border:1px solid #D1D5DB;' +
      'border-radius:8px;padding:10px 12px;font-size:14px;outline:none;">' +
      '<button id="lp-send" style="background:' + COLOR + ';color:#fff;border:none;border-radius:8px;' +
      'padding:0 18px;font-weight:600;cursor:pointer;">Send</button></div>';
    return el;
  }}
  function bubble(role, text) {{
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;justify-content:' + (role === 'lead' ? 'flex-start' : 'flex-end') + ';';
    var b = document.createElement('div');
    b.textContent = text;
    b.style.cssText = 'max-width:78%;padding:10px 14px;border-radius:16px;font-size:14px;line-height:1.5;' +
      (role === 'lead' ? 'background:#fff;color:#111;border:1px solid #E5E7EB;' : 'background:' + COLOR + ';color:#fff;');
    row.appendChild(b);
    document.getElementById('lp-thread').appendChild(row);
    document.getElementById('lp-thread').scrollTop = document.getElementById('lp-thread').scrollHeight;
  }}
  async function start() {{
    if (sessionId) return;
    try {{
      var r = await fetch(API + '/demo/token', {{ method: 'POST' }});
      if (!r.ok) throw new Error('token endpoint HTTP ' + r.status);
      var d = await r.json();
      sessionId = d.session_id; token = d.token;
      authHeaders = {{ 'Content-Type': 'application/json', 'X-Demo-Token': token }};""" + (
        "\n    if (WIDGET_KEY) authHeaders['X-Widget-Key'] = WIDGET_KEY;" if widget_key else ""
    ) + f"""
    }} catch (err) {{
      console.error('[LeadPulse] start failed: ' + (err && err.message));
      bubble('agent', BACKEND_ERROR);
      return;
    }}
    try {{
      var s = await fetch(API + '/webhook/start', {{
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({{ session_id: sessionId, channel: 'web' }}),
      }});
      if (!s.ok) throw new Error('start endpoint HTTP ' + s.status);
      var sd = await s.json();
      bubble('agent', sd.message || 'Hello! How can I help you today?');
    }} catch (err) {{
      console.error('[LeadPulse] start failed: ' + (err && err.message));
      bubble('agent', BACKEND_ERROR);
    }}
  }}
  async function send() {{
    var input = document.getElementById('lp-input');
    var text = input.value.trim();
    if (!text || sending || !sessionId) return;
    sending = true;
    input.value = '';
    bubble('lead', text);
    try {{
      var r = await fetch(API + '/webhook/message', {{
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({{ session_id: sessionId, message: text, channel: 'web' }}),
      }});
      if (!r.ok) throw new Error('message endpoint HTTP ' + r.status);
      var d = await r.json();
      bubble('agent', d.reply || 'I understand. Let me help you with that.');
    }} catch (err) {{
      console.error('[LeadPulse] send failed: ' + (err && err.message));
      bubble('agent', BACKEND_ERROR);
    }}
    sending = false;
  }}
  function toggle() {{
    var p = document.getElementById('lp-panel');
    if (!p) return;
    open = !open;
    p.style.display = open ? 'flex' : 'none';
    if (open) {{ start(); document.getElementById('lp-input').focus(); }}
  }}
  document.addEventListener('DOMContentLoaded', function () {{
    document.body.appendChild(btn());
    document.body.appendChild(panel());
    document.getElementById('lp-send').onclick = send;
    document.getElementById('lp-input').onkeydown = function (e) {{
      if (e.key === 'Enter') send();
    }};
  }});
}})();
</script>"""


def _setup_checklist(
    *,
    org: dict,
    admin_email: str | None,
    admin_password: str | None,
    api_base: str,
    app_hostname: str | None,
    vertical: str,
) -> str:
    """Build the printable post-onboarding checklist."""
    slug = org["slug"]
    subdomain = f"https://{slug}.{app_hostname}" if app_hostname else None
    lines = [
        "=" * 66,
        "LEADPULSE — CLIENT ONBOARDING CHECKLIST",
        "=" * 66,
        f"[x] Organization created       name={org['name']}",
        f"[x] Tenant slug                {slug}  (id={org['id']})",
        f"[x] Plan tier                  {org['plan_tier']}",
        f"[x] Vertical                   {vertical}",
        f"[x] Widget key generated       tenant-bound, embedded in snippet (X-Widget-Key)",
        f"[x] Gemini key stored          encrypted in crm_configs (tenant-scoped)",
    ]
    if admin_email:
        lines.append(f"[x] Dashboard admin user       {admin_email}")
        lines.append(f"    -> temporary password:     {admin_password}")
    else:
        lines.append("[ ] Dashboard admin user       create one via /auth/register or re-run with --admin-email")
    if org.get("notification_phone"):
        lines.append(f"[x] Hot-lead SMS target         {org['notification_phone']} (requires SMS_ENABLED=true)")

    lines += [
        "",
        "1. INSTALL THE WIDGET",
        "   Paste the generated <script> snippet into the client's website just before </body>.",
        "   Widget file: " + ("saved to disk (--widget-out)" if org.get("widget_path") else "printed above"),
        f"   Widget API base: {api_base}",
        "   The widget sends X-Widget-Key on every call, so every conversation is stored",
        "   under THIS tenant automatically — no Host/CNAME setup required.",
        "   Embed on the client's site and hard-refresh to confirm the bubble appears.",
        "",
        "2. TENANT RESOLUTION (optional, for dashboard/branding)",
        "   The widget key already scopes the webhook data to this tenant. For the",
        "   analytics dashboard / custom domains you may still want one of:",
    ]
    if subdomain:
        lines += [
            f"   a) Auto subdomain: add a CNAME from {slug}.{app_hostname} to the API host.",
            "      Then load the widget from that subdomain URL.",
        ]
    lines += [
        "   b) Custom domain: set the org's custom_domain and verify it (TXT record) so",
        "      resolve_tenant_from_host returns the tenant.",
        "",
        "3. BRANDING (optional)",
        "   Set brand_name / logo_url / primary_color on the organizations row to restyle",
        "   the dashboard and prompts.",
        "",
        "4. CRM INTEGRATION (optional)",
        "   Add a crm_configs row for fub / kvcore / ams360 with encrypted credentials so",
        "   qualified leads are pushed automatically.",
        "",
        "5. HOT-LEAD NOTIFICATIONS (optional)",
        "   Set RESEND_API_KEY (+ RESEND_FROM_EMAIL) and SMS_ENABLED=true with Twilio",
        "   creds to get an email/SMS the moment a lead turns hot or books a meeting.",
        "   Email goes to the org_admin created above; SMS to the org notification_phone.",
        "",
        "6. SMOKE TEST",
        f"   Open the widget and send: \"Hi, I'm interested in your services.\"",
        f"   Expect an instant reply. Check /conversation/<session_id> for extracted fields.",
        "",
        "DONE. Client is live — total time should be well under 15 minutes.",
        "=" * 66,
    ]
    return "\n".join(lines)


@dataclass
class OnboardResult:
    org: dict = field(default_factory=dict)
    widget_snippet: str = ""
    checklist: str = ""


async def onboard_client(args: argparse.Namespace) -> OnboardResult:
    from app.database.crud import create_organization, get_organization_by_slug
    from app.database.session import async_session_factory
    from app.integrations.encryption import encrypt_json

    async with async_session_factory() as session:
        slug = await _unique_slug(async_session_factory, args.slug or slugify(args.agency))

        existing = await get_organization_by_slug(session, slug)
        if existing is not None:
            raise SystemExit(f"An organization with slug '{slug}' already exists. Pass --slug to pick another.")

        org = await create_organization(
            session,
            name=args.agency,
            slug=slug,
            plan_tier=args.plan_tier,
        )
        widget_key = secrets.token_urlsafe(32)
        org.widget_key = widget_key
        if args.notification_phone:
            org.notification_phone = args.notification_phone
        org.brand_name = args.brand_name or args.agency
        if args.primary_color:
            org.primary_color = args.primary_color
        if args.logo_url:
            org.logo_url = args.logo_url
        await session.flush()

        # Per-tenant Gemini credentials (encrypted at rest via crm_configs).
        gemini_config = encrypt_json({
            "api_key": args.gemini_key,
            "vertical": args.vertical,
            "business_name": args.brand_name or args.agency,
        }, tenant_id=org.id)
        from app.database.models import CRMConfig

        session.add(CRMConfig(
            organization_id=org.id,
            integration_type="gemini",
            config=gemini_config,
            is_active=True,
        ))

        admin_email = None
        admin_password = None
        if args.admin_email:
            from app.services.auth import create_user

            admin_email = args.admin_email
            admin_password = uuid.uuid4().hex[:16]
            await create_user(
                session,
                email=args.admin_email,
                password=admin_password,
                display_name="Agency Admin",
                role="org_admin",
                organization_id=org.id,
            )

        await session.commit()
        org_id = str(org.id)

    api_base = (args.api_base or "").rstrip("/")
    widget = _widget_snippet(
        tenant_slug=slug,
        api_base=api_base,
        brand_name=args.brand_name or args.agency,
        primary_color=args.primary_color or "",
        title=args.widget_title or DEFAULT_WIDGET_TITLE,
        widget_key=widget_key,
    )

    if args.widget_out:
        with open(args.widget_out, "w", encoding="utf-8") as fh:
            fh.write(widget)
        logger.info("Widget snippet written to %s", args.widget_out)

    org = {
        "id": org_id,
        "name": args.agency,
        "slug": slug,
        "plan_tier": args.plan_tier,
        "widget_key": widget_key,
        "notification_phone": args.notification_phone,
        "widget_path": args.widget_out,
    }
    checklist = _setup_checklist(
        org=org,
        admin_email=admin_email,
        admin_password=admin_password,
        api_base=api_base or "(same-origin)",
        app_hostname=args.app_hostname,
        vertical=args.vertical,
    )
    return OnboardResult(org=org, widget_snippet=widget, checklist=checklist)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Onboard a new LeadPulse client (organization + widget + checklist).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--agency", required=True, help="Agency / client name")
    parser.add_argument(
        "--vertical", required=True, choices=VALID_VERTICALS,
        help="Lead vertical used by prompts & scoring",
    )
    parser.add_argument("--gemini-key", required=True, help="Gemini API key for this tenant")
    parser.add_argument("--slug", help="Tenant slug (defaults to a slugified agency name)")
    parser.add_argument("--brand-name", help="Display brand name (defaults to agency name)")
    parser.add_argument("--primary-color", help="Hex brand color, e.g. #9B6B43")
    parser.add_argument("--logo-url", help="URL to the client's logo")
    parser.add_argument("--plan-tier", default="starter", help="starter | growth | pro")
    parser.add_argument("--admin-email", help="Create an org_admin dashboard user with this email")
    parser.add_argument("--notification-phone", help="Phone number for hot-lead SMS alerts (E.164, e.g. +15550123)")
    parser.add_argument("--widget-out", help="Write the widget snippet to this file")
    parser.add_argument("--api-base", help="Public API base URL the widget should call (e.g. https://api.example.com)")
    parser.add_argument("--app-hostname", help="Auto-provisioned subdomain base, e.g. app.leadpulse.ai")
    parser.add_argument("--widget-title", help="Widget launcher text (default: 'Chat with us')")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(onboard_client(args))
    print("\n" + "=" * 66)
    print("EMBED THIS ON THE CLIENT'S WEBSITE")
    print("=" * 66)
    print(result.widget_snippet)
    print("\n" + result.checklist + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
