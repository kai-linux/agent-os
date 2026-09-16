import http.client
import json
import sys
from pathlib import Path
from threading import Thread

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator.dashboard.server import make_server
from orchestrator.delivery_metrics import observations, operational_snapshot
from orchestrator.delivery_store import DeliveryStore, store_path


def test_dashboard_observations_exclude_private_payloads(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    store = DeliveryStore(store_path(cfg))
    goal = store.upsert(
        "private",
        "Task",
        "Private original instructions",
        metadata={
            "mailbox_payload": "Private original instructions",
            "workspace": "/private/workspace",
        },
    )
    store.begin_attempt(goal["id"], 1, "a", "worker")
    store.finish_attempt("a", {"summary": "Private model output", "status": "complete"})
    exported = str(observations(cfg))
    assert "Private original" not in exported
    assert "Private model output" not in exported
    assert "/private/workspace" not in exported
    snapshot = operational_snapshot(cfg)
    assert snapshot["metrics"]["verified_delivery"]["value"] == 0
    assert snapshot["metrics"]["unknown_cost_attempts"] == 1


@pytest.fixture
def server(tmp_path):
    cfg = {"root_dir": str(tmp_path), "dashboard_bind_address": "127.0.0.1"}
    server = make_server(cfg, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


def request(server, path, host="localhost"):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    connection.request("GET", path, headers={"Host": host})
    response = connection.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def test_real_http_dashboard_and_live_snapshot(server):
    status, headers, page = request(server, "/")
    assert status == 200
    assert b"Delivery portfolio" in page
    assert "sha256-" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    status, _, body = request(server, "/api/delivery")
    assert status == 200
    assert b"proof.operations.v1" in body


def test_historical_reconciliation_is_not_new_delivery_activity(server, tmp_path):
    store = DeliveryStore(store_path({"root_dir": str(tmp_path)}))
    goal = store.upsert(
        "historical-issue",
        "Previously delivered",
        "Historical request",
        metadata={"historical_import": True},
    )
    store.record_evidence(goal["id"], 1, "human_acceptance", True, "test", {})
    assert store.verify(goal["id"])
    status, _, body = request(server, "/api/delivery")
    assert status == 200
    snapshot = json.loads(body)
    assert snapshot["goals"][0]["verified"] is True
    assert snapshot["metrics"]["historical_imports"] == 1
    assert snapshot["metrics"]["verified_delivery"]["denominator"] == 0
    assert snapshot["timeline"] == []


def test_dns_rebinding_host_rejected(server):
    status, _, _ = request(server, "/api/delivery", host="evil.example")
    assert status == 403


def test_missing_route_is_not_a_false_healthy_dashboard(server):
    assert request(server, "/not-an-api")[0] == 404


def test_malformed_host_is_rejected_without_crashing_handler(server):
    assert request(server, "/api/delivery", host="[invalid")[0] == 400


def test_dashboard_cannot_mutate_goals(server):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    connection.request("POST", "/api/delivery", body='{"state":"succeeded"}')
    assert connection.getresponse().status == 405
    connection.close()
