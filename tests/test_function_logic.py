import json
from dataclasses import dataclass

import pytest
import requests

from src.backend import function_logic as logic


@dataclass
class Org:
    organization_id: str = "14"


class Event:
    def __init__(self, args=None, extra=None):
        self.extra_params = {
            "tool_calls": [{"args": args or {}}],
            **(extra or {}),
        }
        self.organization = Org()
        self.access_token = "chask-token"
        self.orchestration_session_uuid = "session"
        self.internal_orchestration_session_uuid = "internal"
        self.event_id = "event"


class MemoryFileStore:
    def __init__(self, files):
        self.files = dict(files)
        self.writes = {}
        self.counter = 0

    def read_json(self, file_uuid):
        return self.files[file_uuid]

    def write_json(self, payload, filename):
        self.counter += 1
        uuid = f"out-{self.counter}"
        self.writes[uuid] = payload
        return uuid


class RecordingAdapter:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.calls = []

    def submit_receipt(self, receipt, idempotency_key, token):
        self.calls.append((receipt, idempotency_key, token))
        response = self.responses.pop(0) if self.responses else {"status": "accepted", "roma_record_id": "roma-1"}
        if isinstance(response, Exception):
            raise response
        return response


def ready(receipt_id="r1", amount=12990, category="COMBUSTIBLE", digest="abc123"):
    return {
        "receipt_id": receipt_id,
        "file_uuid": f"file-{receipt_id}",
        "file_digest": digest,
        "page_index": 0,
        "fecha": "2026-07-20",
        "monto": amount,
        "categoria": category,
        "proveedor": "Copec",
        "numero": "B123",
    }


def confirmation(amount=12990, category="COMBUSTIBLE", batch_hash="batch-hash-v1", version="1"):
    return {
        "schema_version": version,
        "batch_hash": batch_hash,
        "batch_version": version,
        "allowed_categories": ["COMBUSTIBLE", "PEAJE"],
        "confirmed_receipts": [
            {
                "receipt_id": "r1",
                "confirmed_amount": amount,
                "confirmed_category": category,
            }
        ],
    }


def args():
    return {
        "node_id": "265926",
        "ready_receipts_uuid": "ready",
        "confirmation_uuid": "confirmation",
        "batch_hash": "batch-hash-v1",
        "batch_version": "1",
    }


def run_backend(monkeypatch, files, mode="disabled", adapter=None, extra=None, call_args=None):
    monkeypatch.setenv("POMPEYO_ROMA_WRITE_MODE", mode)
    monkeypatch.setenv("POMPEYO_ROMA_BASE_URL", "https://apps2.pompeyo.cl")
    store = MemoryFileStore(files)
    backend = logic.FunctionBackend(
        Event(call_args or args(), extra=extra),
        file_store=store,
        roma_adapter=adapter or RecordingAdapter(),
    )
    summary = json.loads(backend.process_request())
    output = store.writes[summary["results_uuid"]]
    return summary, output, backend.roma_adapter


def test_rejects_when_confirmation_missing(monkeypatch):
    missing_confirmation_args = args()
    missing_confirmation_args.pop("confirmation_uuid")
    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": {"ready_receipts": [ready()]}},
        call_args=missing_confirmation_args,
    )

    assert summary["rejected_business_count"] == 1
    assert output["results"][0]["error_code"] == "CONFIRMATION_MISSING"
    assert adapter.calls == []


def test_rejects_stale_batch_hash(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(batch_hash="old-hash"),
        },
    )

    assert summary["rejected_business_count"] == 1
    assert output["results"][0]["error_code"] == "CONFIRMATION_STALE"
    assert adapter.calls == []


def test_disabled_mode_is_no_write_accepted(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        mode="disabled",
    )

    assert summary["accepted_count"] == 1
    assert output["wrote_to_roma"] is False
    assert output["results"][0]["no_write_reason"] == "disabled_no_write"
    assert adapter.calls == []


def test_shadow_mode_is_no_write(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        mode="shadow",
    )

    assert summary["accepted_count"] == 1
    assert output["results"][0]["no_write_reason"] == "shadow_no_write"
    assert adapter.calls == []


def test_test_execution_forces_no_write_even_live(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        mode="live",
        extra={"is_test": True},
    )

    assert summary["accepted_count"] == 1
    assert output["mode"] == "test"
    assert output["results"][0]["no_write_reason"] == "test_no_write"
    assert adapter.calls == []


def test_auth_redaction_and_live_token_resolution(monkeypatch):
    class Resolver:
        def __init__(self, event):
            self.event = event

        def resolve_positional(self, widget_data, count):
            return ["secret-token"]

    monkeypatch.setattr(logic, "WidgetParamResolver", Resolver)
    adapter = RecordingAdapter()
    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        mode="live",
        adapter=adapter,
        extra={"widget_data": {"widget_param_1": "secret-uuid"}},
    )

    assert summary["accepted_count"] == 1
    assert adapter.calls[0][2] == "secret-token"
    assert output["audit"]["auth"] == "redacted"
    assert "secret-token" not in json.dumps(output)


def test_live_timeout_maps_failed_technical(monkeypatch):
    class Resolver:
        def __init__(self, event):
            self.event = event

        def resolve_positional(self, widget_data, count):
            return ["secret-token"]

    monkeypatch.setattr(logic, "WidgetParamResolver", Resolver)
    adapter = RecordingAdapter([logic.RomaTechnicalError("ROMA_TIMEOUT", "timed out")])
    summary, output, _ = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        mode="live",
        adapter=adapter,
    )

    assert summary["failed_technical_count"] == 1
    assert output["results"][0]["error_code"] == "ROMA_TIMEOUT"


def test_http_adapter_retries_timeouts(monkeypatch):
    calls = []

    class Response:
        status_code = 201
        text = '{"id":"roma-1"}'

        def json(self):
            return {"id": "roma-1"}

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise requests.Timeout("first timeout")
        return Response()

    monkeypatch.setattr(logic.requests, "post", fake_post)
    adapter = logic.PompeyoRomaHttpAdapter("https://roma.example", endpoint_path="/receipts")

    result = adapter.submit_receipt(ready(), "idem", "secret-token")

    assert result["status"] == "accepted"
    assert len(calls) == 2
    assert calls[0][1]["timeout"] == logic.HTTP_TIMEOUT_SECONDS


def test_duplicate_idempotent_replay_is_accepted(monkeypatch):
    class Resolver:
        def __init__(self, event):
            self.event = event

        def resolve_positional(self, widget_data, count):
            return ["secret-token"]

    monkeypatch.setattr(logic, "WidgetParamResolver", Resolver)
    duplicate_response = {"status": "accepted", "roma_record_id": "roma-1", "duplicate": True}
    summary, output, _ = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        mode="live",
        adapter=RecordingAdapter([duplicate_response]),
    )

    assert summary["accepted_count"] == 1
    assert output["results"][0]["duplicate"] is True


def test_partial_failures(monkeypatch):
    files = {
        "ready": {
            "ready_receipts": [
                ready("r1", amount=12990, category="COMBUSTIBLE"),
                ready("r2", amount=5000, category="PEAJE", digest="def456"),
            ]
        },
        "confirmation": {
            **confirmation(),
            "confirmed_receipts": [
                {"receipt_id": "r1", "confirmed_amount": 12990, "confirmed_category": "COMBUSTIBLE"},
                {"receipt_id": "r2", "confirmed_amount": 9999, "confirmed_category": "PEAJE"},
            ],
        },
    }

    summary, output, adapter = run_backend(monkeypatch, files, mode="disabled")

    assert summary["accepted_count"] == 1
    assert summary["rejected_business_count"] == 1
    assert {item["status"] for item in output["results"]} == {"accepted", "rejected_business"}
    assert adapter.calls == []


def test_category_validation(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready(category="NO PERMITIDA")]},
            "confirmation": confirmation(category="NO PERMITIDA"),
        },
    )

    assert summary["rejected_business_count"] == 1
    assert output["results"][0]["error_code"] == "INVALID_CATEGORY"
    assert adapter.calls == []


def test_stable_idempotency_key_for_same_receipt(monkeypatch):
    files = {
        "ready": {"ready_receipts": [ready()]},
        "confirmation": confirmation(),
    }

    _, output1, _ = run_backend(monkeypatch, files, mode="disabled")
    _, output2, _ = run_backend(monkeypatch, files, mode="disabled")

    assert output1["results"][0]["idempotency_key"] == output2["results"][0]["idempotency_key"]
