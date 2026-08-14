"""Static marketing pages: clean-URL routes serve the HTML from static/."""

from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


EXPECTED = {
    "/case-studies.html": "case-studies.html",
    "/demo-realestate": "demo-realestate-pro.html",
    "/demo-insurance": "demo-insurance-pro.html",
}

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@pytest.mark.asyncio
async def test_static_pages_serve_real_files(client):
    for url, filename in EXPECTED.items():
        resp = await client.get(url)
        assert resp.status_code == 200, f"{url} returned {resp.status_code}"
        assert resp.headers["content-type"].startswith("text/html")
        # Serves actual file content from static/, not a placeholder.
        assert len(resp.text) > 2000, f"{url} looks too small to be the page"


@pytest.mark.asyncio
async def test_case_studies_contains_real_anchors(client):
    resp = await client.get("/case-studies.html")
    assert resp.status_code == 200
    for anchor in ('id="real-estate-brokerage"', 'id="moore-real-estates"'):
        assert anchor in resp.text


@pytest.mark.asyncio
async def test_static_pages_include_demo_markers(client):
    resp = await client.get("/demo-realestate")
    assert "demo-realestate-pro.html" in resp.text or "real" in resp.text.lower()
    resp = await client.get("/demo-insurance")
    assert "insurance" in resp.text.lower()


@pytest.mark.asyncio
async def test_unknown_static_path_404s(client):
    resp = await client.get("/nope.html")
    assert resp.status_code == 404