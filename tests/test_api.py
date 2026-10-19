"""The whole lifecycle with a test key: no LiveKit, no phone, no LLM."""
import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from openfury import api, server
    importlib.reload(api)
    importlib.reload(server)
    tenants = {"acme": {"name": "Acme", "concurrency": 2, "calling_hours": ["00:00", "23:59"],
                        "retention_hours": 24, "defaults": {"company_name": "Acme Talent"},
                        "live_key_sha256": api._sha("of_live_x"), "test_key_sha256": api._sha("of_test_x"),
                        "webhook_secret": "whsec_x"}}
    api.TENANTS.parent.mkdir(parents=True, exist_ok=True)
    api.TENANTS.write_text(json.dumps(tenants))
    with TestClient(server.app) as c:
        yield c


H = {"Authorization": "Bearer of_test_x"}
JOB = {"role_name": "Backend Engineer", "role_location": "Bangalore", "work_mode": "hybrid",
       "experience_range": "3 to 6 years", "ctc_max": "30 lakhs per annum", "max_notice_period": "60 days"}


def test_rejects_bad_key(client):
    assert client.get("/v1/account", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_lists_use_cases(client):
    ids = [u["id"] for u in client.get("/v1/use-cases", headers=H).json()["data"]]
    assert {"job-screening", "engineering", "sales", "support", "language-freelancer"} <= set(ids)


def test_validates_job_fields(client):
    r = client.post("/v1/calls", headers=H, json={"use_case": "engineering",
                    "candidate": {"name": "Ravi", "phone": "9876543210"}, "job": {"role_name": "x"}})
    assert r.status_code == 422 and "missing required" in r.json()["error"]["message"]


@pytest.mark.parametrize("slug", ["engineering", "sales", "support", "job-screening"])
def test_test_mode_call_completes(client, slug):
    r = client.post("/v1/calls", headers=H, json={"use_case": slug,
                    "candidate": {"name": "Ravi Kumar", "phone": "9876543210"}, "job": JOB})
    assert r.status_code == 202, r.text
    cid = r.json()["id"]
    for _ in range(60):
        call = client.get(f"/v1/calls/{cid}", headers=H).json()
        if call["status"] == "ended":
            break
        time.sleep(0.5)
    assert call["status"] == "ended" and call["test"] is True
    assert call["use_case"] == slug
    assert call["result"]["verdict"]
    assert "{{" not in json.dumps(call)
    assert call["transcript"][0]["text"].startswith("Hi Ravi")
