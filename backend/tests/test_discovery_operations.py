import importlib.util
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from app import discovery, discovery_runtime as runtime
from app.inventory import load_inventory, save_inventory, update_hardware_availability
from app.main import app
from app.models import ActorIdentity, InventoryRefreshRequest


def inventory_file(tmp_path, count=2):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"devices": {
        f"edge-{number}": {"id": f"edge-{number}", "type": "edge", "display_name": f"edge-{number}",
                            "model": "edge7X0", "serial_number": f"SERIAL{number}", "lab_navigator_id": number}
        for number in range(1, count + 1)
    }, "connections": []}))
    save_inventory(load_inventory(path), path)
    return path


def wiremap(interface, device, remote_interface):
    return {"interface_name": interface, "remote_device": device, "remote_interface_name": remote_interface}


def mock_client(handler):
    client = discovery.LabNavigatorClient(api_key="", base_url="https://lab-navigator.test")
    client.client.close()
    client.client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    return client


def test_shared_graph_fetches_each_device_once_and_apply_reuses_preview(tmp_path):
    path = inventory_file(tmp_path)
    calls = []
    switch = {"id": 11, "name": "switch", "device_type": "switch", "device_model": "Arista-7050SX3-48YC8"}
    sparse = {"id": 11, "name": "switch", "device_type": "switch"}

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/inventory":
            assert json.loads(request.url.params["filters"]) == []
            return httpx.Response(200, json={"devices": [switch]})
        identifier = int(request.url.path.split("/")[-2])
        connections = [] if identifier == 11 else [wiremap("GE1", sparse, f"Eth{identifier}")]
        return httpx.Response(200, json={"connections": connections})

    client = mock_client(handle)
    try:
        preview = discovery.preview_inventory_refresh(
            InventoryRefreshRequest(hardware_ids=["edge-1", "edge-2", "edge-1"]), inventory_path=path, client=client)
    finally:
        client.close()
    assert sorted(calls) == sorted(["/api/inventory", "/api/device/1/wiremap", "/api/device/2/wiremap", "/api/device/11/wiremap"])
    assert preview.hardware_ids == ["edge-1", "edge-2"]
    assert len(preview.inventory.connections) == 2
    assert preview.inventory.devices["switch"].switch_metadata.os_family == "eos"
    status = runtime.operation_status(preview.request_id)
    assert status["state"] == "succeeded"
    assert status["completed_switches"] == status["discovered_switches"] == 1
    assert status["completed_roots"] == 2
    assert status["upstream_calls"] == 4
    assert runtime.operation_status(preview.request_id, status["last_seq"])["logs"] == []

    class NoDiscovery:
        def __getattr__(self, name):
            raise AssertionError("Apply must not perform discovery")

    update_hardware_availability("edge-1", False, ActorIdentity(name="Other user", email="other@example.com"), path)
    applied = discovery.apply_inventory_refresh(
        InventoryRefreshRequest(hardware_ids=preview.hardware_ids, preview_id=preview.preview_id),
        inventory_path=path, client=NoDiscovery())
    assert not next(item for item in applied.inventory.hardware if item.id == "edge-1").available
    assert runtime.operation_status(applied.request_id)["upstream_calls"] == 0
    assert runtime.operation_status(applied.request_id)["parent_request_id"] == preview.request_id
    with pytest.raises(runtime.DiscoveryOperationError) as error:
        discovery.apply_inventory_refresh(InventoryRefreshRequest(hardware_ids=preview.hardware_ids, preview_id=preview.preview_id), inventory_path=path)
    assert error.value.status_code == 410


@pytest.mark.parametrize("failure,expected", [("expired", 410), ("mismatch", 400), ("stale", 409)])
def test_invalid_previews_never_save(tmp_path, monkeypatch, failure, expected):
    path = inventory_file(tmp_path, 1)
    client = mock_client(lambda request: httpx.Response(200, json={"devices": [], "connections": []}))
    try:
        preview = discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"]), inventory_path=path, client=client)
    finally:
        client.close()
    targets = ["edge-1"]
    if failure == "expired":
        runtime._previews[preview.preview_id]["created"] -= runtime.PREVIEW_TTL + 1
    elif failure == "mismatch":
        targets = ["other-edge"]
    else:
        raw = json.loads(path.read_text())
        raw["devices"]["edge-1"]["notes"] = "Another user's edit"
        path.write_text(json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(runtime.DiscoveryOperationError) as error:
        discovery.apply_inventory_refresh(InventoryRefreshRequest(hardware_ids=targets, preview_id=preview.preview_id), inventory_path=path)
    assert error.value.status_code == expected
    assert path.read_bytes() == before
    assert runtime.active_status()["active"] is None


def test_two_users_cannot_overlap_and_status_remains_available(monkeypatch):
    entered, finish = Event(), Event()
    accepted_id = str(uuid4())
    calls = []

    class BlockingClient:
        def search(self, query):
            calls.append(query)
            entered.set()
            assert finish.wait(5)
            return []

        def close(self):
            pass

    monkeypatch.setattr(discovery, "LabNavigatorClient", BlockingClient)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(lambda: TestClient(app).get("/api/lab-navigator/search?q=first", headers={"X-Request-ID": accepted_id}))
        try:
            assert entered.wait(5)
            second_user = TestClient(app)
            active = second_user.get("/api/discovery/active").json()["active"]
            assert active["request_id"] == accepted_id
            assert second_user.get(f"/api/discovery/requests/{accepted_id}").json()["state"] == "running"
            for _ in range(2):
                rejected = second_user.get("/api/lab-navigator/search?q=second")
                assert rejected.status_code == 409
                assert rejected.json()["detail"]["active_request_id"] == accepted_id
                assert rejected.json()["detail"]["code"] == "discovery_busy"
            assert calls == ["first"]
            with pytest.raises(runtime.DiscoveryOperationError) as busy:
                discovery.sync_inventory_for_generate(["edge-1"])
            assert busy.value.code == "discovery_busy"
        finally:
            finish.set()
        response = first.result(timeout=5)
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == accepted_id
    assert runtime.active_status()["active"] is None
    assert runtime.operation_status(accepted_id)["state"] == "succeeded"
    assert TestClient(app).get("/api/lab-navigator/search?q=third").status_code == 200


def test_deadline_does_not_save_and_releases_gate(tmp_path, monkeypatch):
    path = inventory_file(tmp_path, 1)
    original = path.read_bytes()

    class ExpiringClient:
        def close(self):
            pass

        def prepare_discovery(self):
            runtime.current_operation().started -= runtime.OPERATION_SECONDS + 1
            runtime.check_deadline()

    with pytest.raises(runtime.DiscoveryTimeout):
        discovery.apply_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"]), inventory_path=path, client=ExpiringClient())
    assert path.read_bytes() == original
    assert runtime.active_status()["active"] is None
    identifier = next(reversed(runtime._records))
    assert runtime.operation_status(identifier)["state"] == "timed_out"
    monkeypatch.setattr(discovery, "LabNavigatorClient", lambda: ExpiringClient())
    response = TestClient(app).post("/api/hardware/refresh-preview", json={"hardware_ids": ["edge-1"]})
    assert response.status_code == 504
    assert response.json()["detail"]["request_id"]


def test_failures_cached_and_prefetch_concurrency_is_bounded():
    count, maximum = 0, 0
    guard = Lock()
    calls = []

    def handle(request):
        nonlocal count, maximum
        with guard:
            count += 1
            maximum = max(maximum, count)
            calls.append(request.url.path)
        try:
            time.sleep(0.01)
            return httpx.Response(502 if request.url.path.endswith("/1/wiremap") else 200, json={"connections": []})
        finally:
            with guard:
                count -= 1

    client = mock_client(handle)
    try:
        @runtime.discovery_operation("test-prefetch")
        def run():
            client.prefetch_wiremaps(list(range(1, 13)))
            client.prefetch_wiremaps(list(range(1, 13)))
            for _ in range(2):
                with pytest.raises(discovery.DiscoveryError):
                    client.get_wiremap(1)
        run()
    finally:
        client.close()
    assert len(calls) == 12
    assert 1 < maximum <= 4


def test_failed_switch_warning_stays_with_its_component(tmp_path):
    path = inventory_file(tmp_path)
    switches = {number: {"id": number, "name": f"switch-{number}", "device_type": "switch",
                          "device_model": "Dell-3048"} for number in (11, 22, 33)}
    def handle(request):
        if request.url.path == "/api/inventory":
            return httpx.Response(200, json={"devices": list(switches.values())})
        identifier = int(request.url.path.split("/")[-2])
        if identifier == 33:
            return httpx.Response(502)
        peers = {1: [wiremap("GE1", switches[11], "Gi1/1")],
                 2: [wiremap("GE1", switches[22], "Gi1/1")],
                 11: [wiremap("Gi1/48", switches[33], "Gi1/48")], 22: []}
        return httpx.Response(200, json={"connections": peers[identifier]})
    client = mock_client(handle)
    try:
        result = discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1", "edge-2"]), inventory_path=path, client=client)
    finally:
        client.close()
    assert result.summary.targets[0].labels == ["skipped switch wiremaps: switch-33"]
    assert result.summary.targets[1].status == "success"
    assert result.summary.targets[1].labels == []


def test_bulk_fallback_and_new_operations_do_not_reuse_stale_metadata(tmp_path):
    path = inventory_file(tmp_path, 1)
    calls = []
    switch = {"id": 11, "name": "switch", "device_type": "switch", "device_model": "Dell-3048"}
    def handle(request):
        calls.append((request.url.path, str(request.url.query)))
        if request.url.path == "/api/inventory":
            if json.loads(request.url.params["filters"]) == []:
                return httpx.Response(502)
            return httpx.Response(200, json={"devices": [dict(switch)]})
        return httpx.Response(200, json={"connections": [
            wiremap("GE1", {"id": 11, "name": "switch", "device_type": "switch"}, "Gi1/1"),
            wiremap("GE2", {"id": 11, "name": "switch", "device_type": "switch"}, "Gi1/2"),
        ] if "/1/wiremap" in request.url.path else []})
    client = mock_client(handle)
    try:
        first = discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"]), inventory_path=path, client=client)
        assert first.inventory.devices["switch"].model == "Dell-3048"
        assert len([call for call in calls if call[0] == "/api/inventory"]) == 2
        switch["device_model"] = "Dell-4048"
        second = discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"]), inventory_path=path, client=client)
        assert second.inventory.devices["switch"].model == "Dell-4048"
        assert len([call for call in calls if call[0] == "/api/inventory"]) == 4
    finally:
        client.close()


def test_bulk_refresh_preserves_removed_device_and_refreshes_remaining_groups(tmp_path):
    path = inventory_file(tmp_path)
    def handle(request):
        if request.url.path == "/api/device/1/wiremap":
            return httpx.Response(404)
        return httpx.Response(200, json={"devices": [], "connections": []})
    client = mock_client(handle)
    try:
        result = discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1", "edge-2"]), inventory_path=path, client=client)
        assert result.summary.status == "partial"
        assert result.summary.targets[0].labels == ["unavailable devices preserved: edge-1 (Lab Navigator ID 1)"]
        assert result.summary.targets[1].status == "success"
        assert "edge-1" in result.inventory.devices
        with pytest.raises(discovery.DiscoveryDeviceNotFound):
            discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"]), inventory_path=path, client=client)
    finally:
        client.close()


def test_apply_preserves_release_after_preview(tmp_path):
    path = inventory_file(tmp_path, 1)
    actor = ActorIdentity(name="Other user", email="other@example.com")
    update_hardware_availability("edge-1", False, actor, path)
    client = mock_client(lambda request: httpx.Response(200, json={"devices": [], "connections": []}))
    try:
        preview = discovery.preview_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"]), inventory_path=path, client=client)
    finally:
        client.close()
    update_hardware_availability("edge-1", True, actor, path)
    applied = discovery.apply_inventory_refresh(InventoryRefreshRequest(hardware_ids=["edge-1"], preview_id=preview.preview_id), inventory_path=path)
    assert applied.inventory.hardware[0].available
    assert applied.inventory.hardware[0].reservation is None


def test_status_retention_and_nested_operations(monkeypatch):
    @runtime.discovery_operation("outer-test")
    def outer():
        identifier = runtime.current_operation().request_id
        @runtime.discovery_operation("nested-test")
        def inner():
            assert runtime.current_operation().request_id == identifier
        inner()
        return identifier
    identifier = outer()
    record = runtime._records[identifier]
    for number in range(510):
        record.update(message=f"event {number}")
    assert len(runtime.operation_status(identifier)["logs"]) == 500
    record.finished -= runtime.STATUS_TTL + 1
    assert runtime.operation_status(identifier) is None


def test_progress_uses_service_logging_with_request_id(caplog):
    with caplog.at_level("INFO", logger="uvicorn.error"):
        @runtime.discovery_operation("logging-test")
        def operation():
            runtime.progress("metadata", "Loading device metadata")
            return runtime.current_operation().request_id
        identifier = operation()
    assert f"[request_id={identifier}]" in caplog.text
    assert "stage=metadata" in caplog.text
    assert "Discovery succeeded" in caplog.text


@pytest.mark.parametrize("wrapped", [False, True])
def test_frontend_proxy_returns_json_for_socket_timeouts(monkeypatch, wrapped):
    from urllib.error import URLError
    source = Path(__file__).resolve().parents[2] / "deploy" / "frontend_server.py"
    spec = importlib.util.spec_from_file_location("frontend_proxy_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def timeout(*args, **kwargs):
        raise URLError(TimeoutError()) if wrapped else TimeoutError()
    monkeypatch.setattr(module, "urlopen", timeout)
    handler = object.__new__(module.FrontendRequestHandler)
    handler.backend_origin = "http://127.0.0.1:5401"
    handler.path = "/api/hardware/refresh-preview"
    handler.headers = {"X-Request-ID": str(uuid4())}
    handler.command = "POST"
    handler.wfile = io.BytesIO()
    headers, statuses = {}, []
    handler.send_response = statuses.append
    handler.send_header = headers.__setitem__
    handler.end_headers = lambda: None
    handler._proxy_request()
    assert statuses == [504]
    assert headers["Content-Type"] == "application/json"
    assert json.loads(handler.wfile.getvalue())["detail"]["request_id"] == handler.headers["X-Request-ID"]
