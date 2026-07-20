import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from src.backend import function_logic as logic


@dataclass
class Org:
    organization_id: str = "14"


class Event:
    def __init__(self, args=None, extra=None, source="agent"):
        self.extra_params = {
            "tool_calls": [{"args": args or {}}],
            **(extra or {}),
        }
        self.organization = Org()
        self.access_token = "chask-token"
        self.orchestration_session_uuid = "session"
        self.internal_orchestration_session_uuid = "internal"
        self.event_id = "event"
        self.source = source


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
                "category_id": category,
            }
        ],
    }


def args():
    return {
        "node_id": "265926",
        "ready_receipts_uuid": "ready",
        "confirmation_artifact_uuid": "confirmation",
        "batch_hash": "batch-hash-v1",
        "batch_version": "1",
    }


def suite_args():
    return {
        "node_id": "265926",
        "file_uuids": ["uploaded-ready", "uploaded-confirmation"],
        "batch_hash": "batch-hash-v1",
        "batch_version": "1",
    }


def suite_extra(attachments=None):
    return {
        "is_test": True,
        "is_node_test": True,
        "test_execution_uuid": "suite-exec-uuid",
        "explicit_lambda_override": "RegistrarBoletasRomaFn",
        "attachments": attachments
        if attachments is not None
        else [
            {
                "file_uuid": "uploaded-ready",
                "file_name": "analyzer_ready_receipts_output.json",
            },
            {
                "file_uuid": "uploaded-confirmation",
                "source": "test_files/analyzer_confirmation_artifact.json",
            },
        ],
    }


def analyzer_fixtures():
    fixture_dir = Path(__file__).resolve().parents[1] / "test_files"
    return {
        "uploaded-ready": json.loads((fixture_dir / "analyzer_ready_receipts_output.json").read_text()),
        "uploaded-confirmation": json.loads((fixture_dir / "analyzer_confirmation_artifact.json").read_text()),
    }


def run_backend(monkeypatch, files, mode="disabled", adapter=None, extra=None, call_args=None, source="agent"):
    monkeypatch.setenv("POMPEYO_ROMA_WRITE_MODE", mode)
    monkeypatch.setenv("POMPEYO_ROMA_BASE_URL", "https://apps1.pompeyo.cl")
    store = MemoryFileStore(files)
    backend = logic.FunctionBackend(
        Event(call_args or args(), extra=extra, source=source),
        file_store=store,
        roma_adapter=adapter or RecordingAdapter(),
    )
    summary = json.loads(backend.process_request())
    output = store.writes[summary["results_uuid"]]
    return summary, output, backend.roma_adapter


def test_rejects_when_confirmation_missing(monkeypatch):
    missing_confirmation_args = args()
    missing_confirmation_args.pop("confirmation_artifact_uuid")
    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": {"ready_receipts": [ready()]}},
        call_args=missing_confirmation_args,
    )

    assert summary["rejected_business_count"] == 1
    assert output["results"][0]["error_code"] == "CONFIRMATION_MISSING"
    assert adapter.calls == []


def test_accepts_legacy_confirmation_uuid_alias(monkeypatch):
    legacy_args = args()
    legacy_args["confirmation_uuid"] = legacy_args.pop("confirmation_artifact_uuid")

    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        call_args=legacy_args,
    )

    assert summary["accepted_count"] == 1
    assert output["results"][0]["no_write_reason"] == "disabled_no_write"
    assert adapter.calls == []


def test_confirmation_artifact_contract_fixture(monkeypatch):
    fixture_dir = Path(__file__).resolve().parents[1] / "test_files"
    ready_payload = json.loads((fixture_dir / "ready_receipts_contract.json").read_text())
    confirmation_payload = json.loads((fixture_dir / "confirmation_artifact_contract.json").read_text())

    summary, output, adapter = run_backend(
        monkeypatch,
        {
            "ready": ready_payload,
            "confirmation": confirmation_payload,
        },
    )

    assert summary["accepted_count"] == 1
    assert output["results"][0]["receipt_id"] == "receipt-001"
    assert output["results"][0]["status"] == "accepted"
    assert adapter.calls == []


def test_strict_suite_uploads_resolve_ready_and_confirmation_uuids(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        analyzer_fixtures(),
        call_args=suite_args(),
        extra=suite_extra(),
        source="test_cli",
    )

    assert summary["accepted_count"] == 2
    assert output["mode"] == "test"
    assert {item["receipt_id"] for item in output["results"]} == {
        "receipt_111111111111111111111111",
        "receipt_222222222222222222222222",
    }
    assert adapter.calls == []


def test_normal_orchestrator_event_cannot_fallback_to_uploaded_files(monkeypatch):
    extra = suite_extra()
    extra["is_test"] = False

    with pytest.raises(ValueError, match="ready_receipts_uuid"):
        run_backend(
            monkeypatch,
            analyzer_fixtures(),
            call_args=suite_args(),
            extra=extra,
            source="orchestrator",
        )


def test_suite_upload_uuid_mismatch_is_rejected(monkeypatch):
    bad_args = suite_args()
    bad_args["file_uuids"] = ["uploaded-ready", "different-confirmation"]

    with pytest.raises(ValueError, match="ready_receipts_uuid"):
        run_backend(
            monkeypatch,
            analyzer_fixtures(),
            call_args=bad_args,
            extra=suite_extra(),
            source="test_cli",
        )


def test_suite_upload_duplicate_fixture_names_are_rejected(monkeypatch):
    duplicate_ready = [
        {
            "file_uuid": "uploaded-ready",
            "file_name": "analyzer_ready_receipts_output.json",
        },
        {
            "file_uuid": "uploaded-confirmation",
            "source": "test_files/analyzer_ready_receipts_output.json",
        },
    ]

    with pytest.raises(ValueError, match="ready_receipts_uuid"):
        run_backend(
            monkeypatch,
            analyzer_fixtures(),
            call_args=suite_args(),
            extra=suite_extra(duplicate_ready),
            source="test_cli",
        )


def test_explicit_args_win_over_suite_upload_mapping(monkeypatch):
    explicit_args = {
        **args(),
        "file_uuids": ["uploaded-ready", "uploaded-confirmation"],
    }

    summary, output, adapter = run_backend(
        monkeypatch,
        {
            **analyzer_fixtures(),
            "ready": {"ready_receipts": [ready()]},
            "confirmation": confirmation(),
        },
        call_args=explicit_args,
        extra=suite_extra(),
        source="test_cli",
    )

    assert summary["accepted_count"] == 1
    assert output["results"][0]["receipt_id"] == "r1"
    assert adapter.calls == []


def test_suite_event_forces_no_write_even_if_live(monkeypatch):
    summary, output, adapter = run_backend(
        monkeypatch,
        analyzer_fixtures(),
        mode="live",
        call_args=suite_args(),
        extra=suite_extra(),
        source="orchestrator",
    )

    assert summary["accepted_count"] == 2
    assert output["mode"] == "test"
    assert output["wrote_to_roma"] is False
    assert {item["no_write_reason"] for item in output["results"]} == {"test_no_write"}
    assert adapter.calls == []


def test_default_roma_base_url_is_apps1_without_path_query_or_token():
    manifest = (Path(__file__).resolve().parents[1] / "manifest.yml").read_text()
    base_url_line = next(line.strip() for line in manifest.splitlines() if "POMPEYO_ROMA_BASE_URL" in line)

    assert 'POMPEYO_ROMA_BASE_URL: "https://apps1.pompeyo.cl"' in manifest
    assert "apps2.pompeyo.cl" not in manifest
    assert base_url_line == 'POMPEYO_ROMA_BASE_URL: "https://apps1.pompeyo.cl"'
    assert "/" not in base_url_line.removeprefix('POMPEYO_ROMA_BASE_URL: "https://apps1.pompeyo.cl')
    assert "?" not in base_url_line
    assert "token" not in base_url_line.lower()


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
                {"receipt_id": "r1", "confirmed_amount": 12990, "confirmed_category": "COMBUSTIBLE", "category_id": "COMBUSTIBLE"},
                {"receipt_id": "r2", "confirmed_amount": 9999, "confirmed_category": "PEAJE", "category_id": "PEAJE"},
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


def test_ambiguous_analyzer_category_succeeds_when_confirmation_pins_candidate(monkeypatch):
    receipt = {
        "receipt_id": "receipt-ambiguous",
        "file_uuid": "file-ambiguous",
        "source": {
            "file_uuid": "file-ambiguous",
            "source_content_sha256": "sha256:ambiguous",
            "page_index": 0,
            "page_number": 1,
        },
        "proposed_amount": {"numeric_value": 45000, "currency": "CLP"},
        "expense_category": {
            "id": "roma-cat-101",
            "name": "Combustible",
            "status": "resolved",
            "ambiguous": True,
            "candidates": [
                {"id": "roma-cat-101", "name": "Combustible", "confidence": 0.61},
                {"id": "roma-cat-303", "name": "Viajes", "confidence": 0.55},
            ],
        },
    }
    artifact = {
        **confirmation(),
        "confirmed_receipts": [
            {
                "receipt_id": "receipt-ambiguous",
                "confirmed_amount": 45000,
                "confirmed_category": "Combustible",
                "category_id": "roma-cat-101",
            }
        ],
        "allowed_categories": ["roma-cat-101", "roma-cat-303"],
    }

    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": {"ready_receipts": [receipt]}, "confirmation": artifact},
        mode="shadow",
    )

    assert summary["accepted_count"] == 1
    assert output["results"][0]["no_write_reason"] == "shadow_no_write"
    assert output["results"][0]["payload_echo_min"]["category_id"] == "ROMA-CAT-101"
    assert output["results"][0]["category_audit"]["analyzer_category_ambiguous"] is True
    assert adapter.calls == []


def test_unresolved_or_missing_category_id_rejects(monkeypatch):
    receipt = {
        **ready(),
        "expense_category": {
            "name": "Combustible",
            "status": "unresolved",
            "ambiguous": False,
            "candidates": [{"id": "roma-cat-101", "name": "Combustible"}],
        },
    }

    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": {"ready_receipts": [receipt]}, "confirmation": confirmation(category="Combustible")},
    )

    assert summary["rejected_business_count"] == 1
    assert output["results"][0]["error_code"] == "INVALID_CONFIRMED_FIELDS"
    assert "unresolved" in output["results"][0]["error_message"]
    assert adapter.calls == []


def test_confirmed_choice_absent_from_analyzer_candidates_rejects(monkeypatch):
    receipt = {
        **ready(),
        "expense_category": {
            "id": "roma-cat-101",
            "name": "Combustible",
            "status": "resolved",
            "ambiguous": True,
            "candidates": [{"id": "roma-cat-101", "name": "Combustible"}],
        },
    }
    artifact = {
        **confirmation(category="Viajes"),
        "confirmed_receipts": [
            {
                "receipt_id": "r1",
                "confirmed_amount": 12990,
                "confirmed_category": "Viajes",
                "category_id": "roma-cat-303",
            }
        ],
        "allowed_categories": ["roma-cat-101", "roma-cat-303"],
    }

    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": {"ready_receipts": [receipt]}, "confirmation": artifact},
    )

    assert summary["rejected_business_count"] == 1
    assert output["results"][0]["error_code"] == "CONFIRMED_CATEGORY_MISMATCH"
    assert adapter.calls == []


def test_analyzer_output_fixture_validates_and_uses_source_digest_for_idempotency(monkeypatch):
    fixture_dir = Path(__file__).resolve().parents[1] / "test_files"
    ready_payload = json.loads((fixture_dir / "analyzer_ready_receipts_output.json").read_text())
    confirmation_payload = json.loads((fixture_dir / "analyzer_confirmation_artifact.json").read_text())
    receipts = ready_payload["receipts"]

    assert ready_payload["schema_version"] == "pompeyo.receipt_batch.v1"
    assert receipts[0]["source"]["source_content_sha256"] == receipts[1]["source"]["source_content_sha256"]
    assert receipts[0]["source"]["source_content_sha256"].startswith("sha256:") is False
    assert receipts[0]["source"]["page_metadata"] == {
        "page_index": 1,
        "page_range": [1, 1],
        "group_label": "boleta-a",
    }
    assert receipts[1]["source"]["page_metadata"] == {
        "page_index": 2,
        "page_range": [2, 2],
        "group_label": "boleta-b",
    }

    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": ready_payload, "confirmation": confirmation_payload},
        mode="shadow",
    )

    assert summary["accepted_count"] == 2
    first, second = output["results"]
    assert first["payload_echo_min"]["amount"] == "10000"
    assert first["payload_echo_min"]["category_id"] == "10"
    assert first["category_audit"]["analyzer_category_ambiguous"] is False
    assert second["payload_echo_min"]["amount"] == "20000"
    assert second["payload_echo_min"]["category_id"] == "20"
    assert second["category_audit"]["analyzer_category_ambiguous"] is True
    assert first["idempotency_key"] != second["idempotency_key"]
    assert adapter.calls == []


def test_same_file_receipts_with_distinct_page_groups_have_distinct_idempotency_keys(monkeypatch):
    digest = "93cf429feecaa19f840316a9b4906d476a7f4eb069002ead021d334f8b7537bf"
    base_receipt = {
        "source": {
            "file_uuid": "file-1",
            "source_content_sha256": digest,
            "page_metadata": {"page_index": 1, "page_range": [1, 1], "group_label": "boleta-a"},
        },
        "proposed_amount": {"numeric_value": 10000},
        "expense_category": {
            "id": "10",
            "name": "Combustible",
            "status": "resolved",
            "ambiguous": False,
            "candidates": [{"id": "10", "name": "Combustible"}],
        },
    }
    second_receipt = {
        **base_receipt,
        "source": {
            **base_receipt["source"],
            "page_metadata": {"page_index": 2, "page_range": [2, 2], "group_label": "boleta-b"},
        },
    }
    ready_payload = {
        "schema_version": "pompeyo.receipt_batch.v1",
        "receipts": [
            {"receipt_id": "receipt-a", **base_receipt},
            {"receipt_id": "receipt-b", **second_receipt},
        ],
    }
    confirmation_payload = {
        **confirmation(),
        "allowed_categories": ["10"],
        "confirmed_receipts": [
            {
                "receipt_id": "receipt-a",
                "confirmed_amount": 10000,
                "confirmed_category": "Combustible",
                "confirmed_category_id": "10",
            },
            {
                "receipt_id": "receipt-b",
                "confirmed_amount": 10000,
                "confirmed_category": "Combustible",
                "confirmed_category_id": "10",
            },
        ],
    }

    summary, output, adapter = run_backend(
        monkeypatch,
        {"ready": ready_payload, "confirmation": confirmation_payload},
        mode="shadow",
    )

    assert summary["accepted_count"] == 2
    assert output["results"][0]["idempotency_key"] != output["results"][1]["idempotency_key"]
    assert adapter.calls == []


def test_stable_idempotency_key_for_same_receipt(monkeypatch):
    files = {
        "ready": {"ready_receipts": [ready()]},
        "confirmation": confirmation(),
    }

    _, output1, _ = run_backend(monkeypatch, files, mode="disabled")
    _, output2, _ = run_backend(monkeypatch, files, mode="disabled")

    assert output1["results"][0]["idempotency_key"] == output2["results"][0]["idempotency_key"]
