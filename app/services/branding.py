"""Branding / white-labeling service.

Derives a full palette from the tenant's ``primary_color``,
returns defaults when no branding is configured.
"""

import re
from dataclasses import dataclass
from typing import Self


@dataclass
class BrandingConfig:
    brand_name: str = "LeadPulse"
    logo_url: str = ""
    primary_color: str = "#4F46E5"
    primary_light: str = "#818CF8"
    primary_dark: str = "#3730A3"
    primary_soft: str = "#EEF2FF"
    text_on_primary: str = "#FFFFFF"
    custom_domain: str = ""
    has_branding: bool = False


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _blend(hex_color: str, factor: float, with_white: bool = True) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    target = (255, 255, 255) if with_white else (0, 0, 0)
    nr = int(r + (target[0] - r) * factor)
    ng = int(g + (target[1] - g) * factor)
    nb = int(b + (target[2] - b) * factor)
    return _rgb_to_hex(nr, ng, nb)


def _luminance(r: int, g: int, b: int) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def derive_palette(primary_color: str | None) -> dict[str, str]:
    if not primary_color or not re.match(r"^#[0-9a-fA-F]{6}$", primary_color):
        return {}
    light = _blend(primary_color, 0.4, with_white=True)
    dark = _blend(primary_color, 0.3, with_white=False)
    soft = _blend(primary_color, 0.85, with_white=True)
    r, g, b = _hex_to_rgb(primary_color)
    text_on_primary = "#FFFFFF" if _luminance(r, g, b) < 140 else "#1A1A1A"
    return {
        "primary_light": light,
        "primary_dark": dark,
        "primary_soft": soft,
        "text_on_primary": text_on_primary,
    }


def get_branding(
    brand_name: str | None = None,
    logo_url: str | None = None,
    primary_color: str | None = None,
    custom_domain: str | None = None,
) -> BrandingConfig:
    palette = derive_palette(primary_color) if primary_color else {}
    has_branding = bool(brand_name or logo_url or primary_color)
    return BrandingConfig(
        brand_name=brand_name or "LeadPulse",
        logo_url=logo_url or "",
        primary_color=primary_color or "#4F46E5",
        primary_light=palette.get("primary_light", "#818CF8"),
        primary_dark=palette.get("primary_dark", "#3730A3"),
        primary_soft=palette.get("primary_soft", "#EEF2FF"),
        text_on_primary=palette.get("text_on_primary", "#FFFFFF"),
        custom_domain=custom_domain or "",
        has_branding=has_branding,
    )


def branding_css_variables(b: BrandingConfig) -> str:
    return f"""
:root {{
  --brand-name: "{b.brand_name}";
  --brand-primary: {b.primary_color};
  --brand-primary-light: {b.primary_light};
  --brand-primary-dark: {b.primary_dark};
  --brand-primary-soft: {b.primary_soft};
  --brand-text-on-primary: {b.text_on_primary};
  --brand-logo-url: "{b.logo_url}";
}}""".strip()


def apply_branding_to_html(html: str, b: BrandingConfig) -> str:
    css_vars = branding_css_variables(b)
    injected = f"<style id=\"branding-vars\">{css_vars}</style>"
    html = html.replace("</head>", f"{injected}\n</head>")
    html = html.replace("LeadPulse — Analytics Dashboard", f"{b.brand_name} — Dashboard")
    html = html.replace(
        '<h1>Lead<span>Pulse</span> Analytics</h1>',
        f'<h1 id="brand-title" style="color:var(--brand-primary)">{b.brand_name}</h1>',
    )
    return html
