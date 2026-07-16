"""W3.4 无构建链前端：静态服务、鉴权边界与设计禁区。"""
from __future__ import annotations

from pathlib import Path
import re

from fastapi.testclient import TestClient

from src.webapp import DeviceRegistry, create_app


TOKEN = "test-web-token"
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "webapp" / "static"


def test_static_frontend_is_public_but_api_and_non_get_requests_are_not() -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    with TestClient(app) as client:
        index = client.get("/")
        stylesheet = client.get("/static/style.css")
        script = client.get("/static/app.js")
        protected_api = client.get("/api/status")
        post_index = client.post("/")
        post_static = client.post("/static/app.js")

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert 'id="global-status-bar"' in index.text
    assert 'id="estop-button"' in index.text
    assert 'id="token-input"' in index.text
    assert 'id="event-log"' in index.text
    assert 'type="module" src="/static/app.js"' in index.text

    assert stylesheet.status_code == 200
    assert script.status_code == 200

    assert protected_api.status_code == 401
    assert protected_api.json()["error"]["error_code"] == (
        "L3.AUTHENTICATION_FAILED"
    )
    assert post_index.status_code == 401
    assert post_static.status_code == 401


def test_css_enforces_design_prohibitions_and_palette() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    normalized = css.lower()

    for forbidden in ("linear-gradient", "box-shadow", "backdrop-filter"):
        assert forbidden not in normalized

    radius_values = re.findall(r"border-radius\s*:\s*([^;]+);", normalized)
    assert radius_values
    for value in radius_values:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)px", value.strip())
        assert match is not None, f"border-radius 必须使用 px：{value}"
        assert float(match.group(1)) <= 6.0

    allowed_colors = {
        "#FFFFFF",
        "#F5F5F7",
        "#D2D2D7",
        "#1D1D1F",
        "#6E6E73",
        "#0066CC",
        "#D70015",
        "#28A745",
    }
    used_colors = {
        match.group(0).upper()
        for match in re.finditer(r"#[0-9a-fA-F]{6}\b", css)
    }
    assert used_colors == allowed_colors


def test_static_assets_are_offline_and_avoid_marketing_or_emoji_chrome() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    combined = index + script

    assert "https://" not in index
    assert "http://" not in index
    assert "hero" not in combined.lower()
    assert "欢迎使用" not in combined
    assert re.search(r"[\U0001F300-\U0001FAFF]", combined) is None
