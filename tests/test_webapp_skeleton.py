"""W3.0 Web 服务骨架：全测试只使用空 mock 注册表。"""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from src.hardware.errors import L3Error
from src.schema_export import SCHEMA_VERSION
from src.system_estop import SystemEstop
from src.webapp import DeviceRegistry, create_app


TOKEN = "test-web-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class _WebTestError(L3Error):
    error_code = "L3.WEB_TEST"
    severity = "warning"
    recoverable = True
    suggested_action = "Correct the test request."
    suggested_action_zh = "修正测试请求。"


def test_health_is_public_and_reports_mock_mode() -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "version": SCHEMA_VERSION,
        "mock": True,
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
    ],
)
def test_missing_or_wrong_token_returns_structured_401(
    headers: dict[str, str],
) -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    with TestClient(app) as client:
        response = client.get("/api/any-protected-path", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    detail = response.json()["error"]
    assert detail["error_code"] == "L3.AUTHENTICATION_FAILED"
    assert detail["human_message"]
    assert detail["agent_message"]


def test_correct_token_passes_authentication() -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    with TestClient(app) as client:
        response = client.get("/api/not-registered", headers=AUTH_HEADERS)

    assert response.status_code == 404
    assert response.status_code != 401


def test_l3_error_handler_returns_all_structured_fields() -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    @app.get("/test/l3-error")
    def raise_l3_error() -> None:
        raise _WebTestError("测试错误", "test error for agent")

    with TestClient(app) as client:
        response = client.get("/test/l3-error", headers=AUTH_HEADERS)

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "error_code": "L3.WEB_TEST",
            "human_message": "测试错误",
            "agent_message": "test error for agent",
            "severity": "warning",
            "recoverable": True,
            "suggested_action": "Correct the test request.",
            "suggested_action_zh": "修正测试请求。",
        }
    }


def test_unexpected_error_is_structured_and_hides_exception_text() -> None:
    app = create_app(DeviceRegistry.from_mocks(), token=TOKEN)

    @app.get("/test/unexpected-error")
    def raise_unexpected_error() -> None:
        raise RuntimeError("private traceback detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/unexpected-error", headers=AUTH_HEADERS)

    assert response.status_code == 500
    assert response.json()["error"]["error_code"] == "L3.INTERNAL_ERROR"
    assert "private traceback detail" not in response.text


def test_create_app_instances_do_not_share_registry_or_routes() -> None:
    first_registry = DeviceRegistry.from_mocks()
    second_registry = DeviceRegistry.from_mocks()
    first_app = create_app(first_registry, token="first-token")
    second_app = create_app(second_registry, token="second-token")

    @first_app.get("/test/first-only")
    def first_only() -> dict[str, bool]:
        return {"first": True}

    assert first_app is not second_app
    assert first_app.state.registry is first_registry
    assert second_app.state.registry is second_registry

    with (
        TestClient(first_app) as first_client,
        TestClient(second_app) as second_client,
    ):
        first_response = first_client.get(
            "/test/first-only",
            headers={"Authorization": "Bearer first-token"},
        )
        second_response = second_client.get(
            "/test/first-only",
            headers={"Authorization": "Bearer second-token"},
        )
        wrong_app_token = second_client.get(
            "/test/first-only",
            headers={"Authorization": "Bearer first-token"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 404
    assert wrong_app_token.status_code == 401


def test_registry_mock_constructor_has_no_real_backends() -> None:
    registry = DeviceRegistry(estop=SystemEstop(), mock=True)

    assert registry.mock is True
    assert registry.gantry is None
    assert registry.relay is None
    assert registry.gripper is None
    assert registry.heater is None
    assert registry.spincoater is None
    assert registry.pipette is None
    assert registry.linear_stage is None
    assert isinstance(registry.estop, SystemEstop)


def test_registry_real_constructor_is_an_explicit_placeholder() -> None:
    with pytest.raises(NotImplementedError):
        DeviceRegistry.from_config()
