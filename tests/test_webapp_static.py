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
    assert (
        'type="module" src="/static/app.js?v=20260729-spin-accel1"'
        in index.text
    )
    assert (
        'href="/static/style.css?v=20260729-summary-gridfix2"'
        in index.text
    )

    assert stylesheet.status_code == 200
    assert script.status_code == 200
    assert '速度：${formatMetric(group.stage_2_speed_rpm, "RPM")}' in script.text
    assert '时间：${formatMetric(group.stage_2_time_s, "s")}' in script.text
    assert (
        '温度：${formatMetric(group.annealing_temperature_c, "°C")}'
        in script.text
    )
    assert '时间：${formatMetric(group.annealing_time_s, "s")}' in script.text

    assert protected_api.status_code == 401
    assert protected_api.json()["error"]["error_code"] == (
        "L3.AUTHENTICATION_FAILED"
    )
    assert post_index.status_code == 401
    assert post_static.status_code == 401


def test_index_contains_all_seven_device_panels_and_safety_controls() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    devices = re.findall(
        r'<article\b[^>]*\bdata-device="([^"]+)"[^>]*>',
        index,
    )
    assert devices == [
        "gantry",
        "heater",
        "spincoater",
        "pipette",
        "linear_stage",
        "relay",
        "gripper",
    ]
    assert len(devices) == len(set(devices))
    assert index.count('data-role="operation-id"') == 7
    assert index.count('data-role="message"') == 7
    assert 'id="heater-chart"' in index
    assert 'id="spincoater-chart"' in index
    assert "当前设备无实测转速反馈" in index
    assert "面板于 W3.5 接入" not in index

    estop_tag = re.search(r'<button\b[^>]*id="estop-button"[^>]*>', index)
    stage_stop_tag = re.search(
        r'<button\b[^>]*id="linear-stage-stop-button"[^>]*>',
        index,
    )
    assert estop_tag is not None
    assert stage_stop_tag is not None
    assert "disabled" not in estop_tag.group(0)
    assert "disabled" not in stage_stop_tag.group(0)
    assert 'data-always-enabled="true"' in stage_stop_tag.group(0)


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


def test_device_grid_prioritizes_gantry_with_heater_and_gripper_on_right() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert '.device-card[data-device="gantry"]' in css
    assert '.device-card[data-device="heater"]' in css
    assert '.device-card[data-device="gripper"]' in css
    assert "grid-column: 1 / span 2;" in css
    assert "grid-column: 3;" in css


def test_static_assets_are_offline_and_avoid_marketing_or_emoji_chrome() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    combined = index + script

    assert "https://" not in index
    assert "http://" not in index
    assert "hero" not in combined.lower()
    assert "欢迎使用" not in combined
    assert re.search(r"[\U0001F300-\U0001FAFF]", combined) is None


def test_dashboard_refactor_exposes_compact_header_and_grouped_builder() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    header = re.search(
        r'<header\b[^>]*id="global-status-bar".*?</header>',
        index,
        re.DOTALL,
    )
    assert header is not None
    assert 'class="header-auth"' in header.group(0)
    assert 'id="token-input"' in header.group(0)
    assert 'id="estop-button"' in header.group(0)

    assert 'class="operations-overview"' in index
    assert 'class="builder-parameter-sections"' in index
    assert ">旋涂参数<" in index
    assert ">液体处理<" in index
    assert ">热处理<" in index
    assert 'data-group-repeat-summary' in index


def test_dashboard_refactor_has_requested_responsive_breakpoints() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert "@media (max-width: 1499px)" in css
    assert "@media (max-width: 1099px)" in css
    assert "@media (max-width: 799px)" in css
    assert ".header-auth" in css
    assert ".builder-parameter-sections" in css
    assert ".relay-actions button[data-relay-on=\"true\"]" in css
    assert ".relay-actions button[data-relay-on=\"false\"]" in css
    assert "grid-template-columns: repeat(12, minmax(0, 1fr));" in css
    assert "grid-column: 1 / span 8;" in css
    assert "grid-column: 9 / span 4;" in css
