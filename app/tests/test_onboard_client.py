import sys

import pytest

sys.path.insert(0, ".")

from scripts.onboard_client import (  # noqa: E402
    DEFAULT_WIDGET_TITLE,
    VALID_VERTICALS,
    _setup_checklist,
    _widget_snippet,
    parse_args,
    slugify,
)


class TestSlugify:
    def test_basic(self):
        assert slugify("Bella Vista Realty") == "bella-vista-realty"

    def test_strips_special_chars(self):
        assert slugify("   Acme!! Group  ") == "acme-group"

    def test_empty_falls_back(self):
        assert slugify("###") == "client"


class TestWidgetSnippet:
    def _snippet(self):
        return _widget_snippet(
            tenant_slug="bella-vista-realty",
            api_base="https://api.example.com",
            brand_name="Bella Vista Realty",
            primary_color="#9B6B43",
            title="Chat with us",
        )

    def test_embeds_tenant_slug(self):
        assert "bella-vista-realty" in self._snippet()

    def test_embeds_api_base(self):
        assert "https://api.example.com" in self._snippet()

    def test_embeds_brand_color(self):
        snippet = self._snippet()
        assert "#9B6B43" in snippet
        assert "Bella Vista Realty" in snippet

    def test_uses_webhook_endpoints(self):
        snippet = self._snippet()
        assert "/webhook/message" in snippet
        assert "/webhook/start" in snippet

    def test_default_title(self):
        snippet = _widget_snippet(
            tenant_slug="x", api_base="", brand_name="", primary_color="", title=DEFAULT_WIDGET_TITLE
        )
        assert DEFAULT_WIDGET_TITLE in snippet


class TestChecklist:
    def _checklist(self):
        return _setup_checklist(
            org={
                "name": "Bella Vista Realty",
                "slug": "bella-vista-realty",
                "id": "abc-123",
                "plan_tier": "starter",
                "widget_path": None,
            },
            admin_email="owner@bv.com",
            admin_password="temp-pass",
            api_base="https://api.example.com",
            app_hostname="app.leadpulse.ai",
            vertical="real_estate",
        )

    def test_includes_org_details(self):
        checklist = self._checklist()
        assert "Bella Vista Realty" in checklist
        assert "bella-vista-realty" in checklist
        assert "starter" in checklist

    def test_includes_admin_credentials(self):
        checklist = self._checklist()
        assert "owner@bv.com" in checklist
        assert "temp-pass" in checklist

    def test_includes_subdomain_cname(self):
        assert "bella-vista-realty.app.leadpulse.ai" in self._checklist()

    def test_missing_admin_notes_manual_step(self):
        checklist = _setup_checklist(
            org={"name": "X", "slug": "x", "id": "1", "plan_tier": "starter", "widget_path": None},
            admin_email=None,
            admin_password=None,
            api_base="https://api.example.com",
            app_hostname=None,
            vertical="generic",
        )
        assert "--admin-email" in checklist


class TestParseArgs:
    def test_requires_vertical_choice(self):
        assert VALID_VERTICALS == ("generic", "real_estate", "insurance")

    def test_valid_vertical_accepted(self):
        args = parse_args([
            "--agency", "Acme", "--vertical", "real_estate", "--gemini-key", "k",
        ])
        assert args.agency == "Acme"
        assert args.vertical == "real_estate"
        assert args.plan_tier == "starter"

    def test_invalid_vertical_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--agency", "Acme", "--vertical", "nonsense", "--gemini-key", "k"])
