"""
RegistrarBoletasRomaFn - guarded ROMA receipt writer for Pompeyo.

This Lambda only writes in POMPEYO_ROMA_WRITE_MODE=live, outside Chask test
executions, after a persisted confirmation artifact matches the exact batch
hash/version and confirms every receipt amount/category.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Protocol

import requests

try:
    from api.files_requests import files_api_manager
except Exception:  # pragma: no cover - local unit tests inject a file store.
    files_api_manager = None

try:
    from api.widget_resolver import WidgetParamResolver
except Exception:  # pragma: no cover - local unit tests inject token behavior.
    WidgetParamResolver = None

try:
    from chask_foundation.backend.models import OrchestrationEvent
except Exception:  # pragma: no cover - local unit tests use lightweight events.
    OrchestrationEvent = Any


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ROMA_WRITE_MODES = {"disabled", "shadow", "live"}
DEFAULT_ROMA_WRITE_MODE = "disabled"
RESULTS_FILENAME = "roma_submission_results.json"
HTTP_TIMEOUT_SECONDS = (3.05, 20)
MAX_RETRIES = 1
CATEGORY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{1,63}$")
UNRESOLVED_ENDPOINT_CODE = "ROMA_ENDPOINT_UNRESOLVED"


class FileStore(Protocol):
    def read_json(self, file_uuid: str) -> Dict[str, Any]:
        ...

    def write_json(self, payload: Dict[str, Any], filename: str) -> str:
        ...


class RomaAdapter(Protocol):
    def submit_receipt(
        self,
        receipt: Dict[str, Any],
        idempotency_key: str,
        token: str,
    ) -> Dict[str, Any]:
        ...


@dataclass(frozen=True)
class ReceiptValidation:
    receipt: Dict[str, Any]
    normalized_amount: str
    normalized_category: str
    idempotency_key: str


class ChaskFileStore:
    def __init__(self, orchestration_event: OrchestrationEvent):
        if files_api_manager is None:
            raise RuntimeError("files_api_manager is unavailable")
        self.orchestration_event = orchestration_event

    def read_json(self, file_uuid: str) -> Dict[str, Any]:
        files = files_api_manager.call(
            "get_all_files_for_session",
            orchestration_session_uuid=self.orchestration_event.orchestration_session_uuid,
            internal_orchestration_session_uuid=self.orchestration_event.internal_orchestration_session_uuid,
            access_token=self.orchestration_event.access_token,
            organization_id=self.orchestration_event.organization.organization_id,
        ).get("files", [])
        file_record = next((item for item in files if item.get("file_uuid") == file_uuid), None)
        if not file_record:
            raise ValueError(f"File UUID not found in session: {file_uuid}")

        response = requests.get(file_record["file_url"], timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        return json.loads(response.content.decode("utf-8"))

    def write_json(self, payload: Dict[str, Any], filename: str) -> str:
        buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        buf.name = filename
        response = files_api_manager.call(
            "upload_file",
            file=buf,
            orchestration_session_uuids=[self.orchestration_event.orchestration_session_uuid],
            internal_orchestration_session_uuid=self.orchestration_event.internal_orchestration_session_uuid,
            shared=False,
            access_token=self.orchestration_event.access_token,
            organization_id=self.orchestration_event.organization.organization_id,
        )
        if isinstance(response, list):
            response = response[0] if response else {}
        status_code = response.get("status_code", 201)
        if status_code not in (200, 201):
            raise RuntimeError(f"Failed to upload results file: {response.get('error', response)}")
        return response["file_uuid"]


class PompeyoRomaHttpAdapter:
    """
    Thin adapter around the future ROMA receipt endpoint.

    The local repository does not prove the exact receipt endpoint or category
    schema. Therefore live submission remains unresolved unless a production
    mapping is explicitly injected by code/config review. This prevents guessing
    and accidentally calling a wrong ROMA route.
    """

    def __init__(self, base_url: str, endpoint_path: Optional[str] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.endpoint_path = endpoint_path

    def submit_receipt(
        self,
        receipt: Dict[str, Any],
        idempotency_key: str,
        token: str,
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise RomaTechnicalError("ROMA_BASE_URL_MISSING", "ROMA base URL is not configured")
        if not self.endpoint_path:
            raise RomaTechnicalError(
                UNRESOLVED_ENDPOINT_CODE,
                "ROMA receipt endpoint/category mapping is unresolved locally",
            )

        url = f"{self.base_url}/{self.endpoint_path.lstrip('/')}"
        payload = self._build_payload(receipt, idempotency_key)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                return self._map_response(response)
            except requests.Timeout as exc:
                last_error = exc
                if attempt >= MAX_RETRIES:
                    raise RomaTechnicalError("ROMA_TIMEOUT", "ROMA request timed out") from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= MAX_RETRIES:
                    raise RomaTechnicalError("ROMA_REQUEST_FAILED", str(exc)[:300]) from exc

        raise RomaTechnicalError("ROMA_REQUEST_FAILED", str(last_error)[:300])

    def _build_payload(self, receipt: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        return {
            "idempotency_key": idempotency_key,
            "receipt_id": receipt.get("receipt_id"),
            "file_uuid": receipt.get("file_uuid"),
            "date": receipt.get("fecha") or receipt.get("date"),
            "amount": receipt.get("monto") or receipt.get("amount"),
            "supplier": receipt.get("proveedor") or receipt.get("supplier"),
            "document_number": receipt.get("numero") or receipt.get("document_number"),
            "document_type": receipt.get("tipo_documento") or receipt.get("document_type"),
            "category": receipt.get("categoria") or receipt.get("category"),
            "detail": receipt.get("detalle") or receipt.get("detail"),
        }

    def _map_response(self, response: requests.Response) -> Dict[str, Any]:
        body: Dict[str, Any]
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:300]}

        if 200 <= response.status_code < 300:
            return {
                "status": "accepted",
                "roma_record_id": body.get("id") or body.get("roma_record_id"),
                "message": body.get("message") or body.get("msj") or "OK",
            }
        if response.status_code == 409:
            return {
                "status": "accepted",
                "roma_record_id": body.get("id") or body.get("roma_record_id"),
                "duplicate": True,
                "message": body.get("message") or "duplicate idempotent replay",
            }
        if 400 <= response.status_code < 500:
            return {
                "status": "rejected_business",
                "error_code": body.get("code") or f"ROMA_{response.status_code}",
                "error_message": body.get("message") or body.get("error") or response.text[:300],
            }
        raise RomaTechnicalError(
            f"ROMA_{response.status_code}",
            body.get("message") or body.get("error") or response.text[:300],
        )


class RomaTechnicalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FunctionBackend:
    def __init__(
        self,
        orchestration_event: OrchestrationEvent,
        file_store: Optional[FileStore] = None,
        roma_adapter: Optional[RomaAdapter] = None,
    ):
        self.orchestration_event = orchestration_event
        self.file_store = file_store or ChaskFileStore(orchestration_event)
        self.roma_adapter = roma_adapter or PompeyoRomaHttpAdapter(
            base_url=os.getenv("POMPEYO_ROMA_BASE_URL", ""),
            endpoint_path=None,
        )
        logger.info(
            "Initialized RegistrarBoletasRomaFn org=%s",
            getattr(getattr(orchestration_event, "organization", None), "organization_id", "unknown"),
        )

    def process_request(self) -> str:
        tool_args = self._with_test_defaults(self._extract_tool_args())
        mode = self._resolve_mode()
        is_test = self._is_test_execution()

        ready_uuid = self._require_arg(tool_args, "ready_receipts_uuid")
        confirmation_uuid = tool_args.get("confirmation_uuid")
        batch_hash = self._require_arg(tool_args, "batch_hash")
        batch_version = str(self._require_arg(tool_args, "batch_version"))

        ready_payload = self.file_store.read_json(str(ready_uuid))
        ready_receipts = self._extract_receipts(ready_payload)
        confirmation = self.file_store.read_json(str(confirmation_uuid)) if confirmation_uuid else None

        logger.info(
            json.dumps(
                {
                    "event": "roma_receipt_writer_start",
                    "mode": "test" if is_test else mode,
                    "ready_receipts_uuid": ready_uuid,
                    "confirmation_uuid_present": bool(confirmation_uuid),
                    "batch_hash": self._short_hash(batch_hash),
                    "batch_version": batch_version,
                    "receipt_count": len(ready_receipts),
                    "auth": "not_resolved_until_live" if mode == "live" and not is_test else "not_required",
                },
                ensure_ascii=False,
            )
        )

        validation_results = self._validate_confirmation(
            ready_receipts=ready_receipts,
            confirmation=confirmation,
            expected_batch_hash=str(batch_hash),
            expected_batch_version=batch_version,
        )

        results: List[Dict[str, Any]] = []
        validated = [item for item in validation_results if isinstance(item, ReceiptValidation)]
        results.extend(item for item in validation_results if isinstance(item, dict))

        should_write = mode == "live" and not is_test
        token = self._resolve_token() if should_write and validated else ""

        for item in validated:
            if not should_write:
                results.append(self._no_write_result(item, mode, is_test))
                continue
            results.append(self._submit_one(item, token))

        output = self._build_output(
            mode=mode,
            is_test=is_test,
            batch_hash=str(batch_hash),
            batch_version=batch_version,
            results=results,
        )
        results_uuid = self.file_store.write_json(output, RESULTS_FILENAME)
        summary = dict(output["summary"])
        summary["results_uuid"] = results_uuid

        logger.info(
            json.dumps(
                {
                    "event": "roma_receipt_writer_complete",
                    "mode": "test" if is_test else mode,
                    "summary": summary,
                    "results_uuid": results_uuid,
                    "token": "redacted",
                },
                ensure_ascii=False,
            )
        )
        return json.dumps(summary, ensure_ascii=False, sort_keys=True)

    def _submit_one(self, item: ReceiptValidation, token: str) -> Dict[str, Any]:
        receipt_id = item.receipt["receipt_id"]
        try:
            adapter_result = self.roma_adapter.submit_receipt(
                receipt=item.receipt,
                idempotency_key=item.idempotency_key,
                token=token,
            )
        except RomaTechnicalError as exc:
            return self._technical_failure(receipt_id, item.idempotency_key, exc.code, exc.message)
        except Exception as exc:
            return self._technical_failure(receipt_id, item.idempotency_key, "ROMA_UNEXPECTED_ERROR", str(exc)[:300])

        status = adapter_result.get("status")
        if status == "accepted":
            return {
                "receipt_id": receipt_id,
                "status": "accepted",
                "roma_record_id": adapter_result.get("roma_record_id"),
                "idempotency_key": item.idempotency_key,
                "duplicate": bool(adapter_result.get("duplicate")),
                "payload_echo_min": self._payload_echo_min(item.receipt, item.idempotency_key),
            }
        if status == "rejected_business":
            return {
                "receipt_id": receipt_id,
                "status": "rejected_business",
                "error_code": adapter_result.get("error_code", "ROMA_BUSINESS_REJECTED"),
                "error_message": adapter_result.get("error_message", "ROMA rejected the receipt"),
                "idempotency_key": item.idempotency_key,
                "payload_echo_min": self._payload_echo_min(item.receipt, item.idempotency_key),
            }
        return self._technical_failure(
            receipt_id,
            item.idempotency_key,
            "ROMA_ADAPTER_INVALID_RESULT",
            f"Unexpected adapter status: {status}",
        )

    def _validate_confirmation(
        self,
        ready_receipts: List[Dict[str, Any]],
        confirmation: Optional[Dict[str, Any]],
        expected_batch_hash: str,
        expected_batch_version: str,
    ) -> List[ReceiptValidation | Dict[str, Any]]:
        if not ready_receipts:
            return [self._business_reject(None, "NO_READY_RECEIPTS", "No ready receipts were provided")]
        if not confirmation:
            return [
                self._business_reject(
                    receipt.get("receipt_id"),
                    "CONFIRMATION_MISSING",
                    "Persisted confirmation artifact is required before ROMA writes",
                )
                for receipt in ready_receipts
            ]

        confirmation_hash = str(confirmation.get("batch_hash") or "")
        confirmation_version = str(confirmation.get("batch_version") or confirmation.get("schema_version") or "")
        if confirmation_hash != expected_batch_hash or confirmation_version != expected_batch_version:
            return [
                self._business_reject(
                    receipt.get("receipt_id"),
                    "CONFIRMATION_STALE",
                    "Confirmation artifact does not match the requested batch hash/version",
                )
                for receipt in ready_receipts
            ]

        confirmations = self._confirmation_map(confirmation)
        allowed_categories = self._allowed_categories(confirmation)
        validated: List[ReceiptValidation | Dict[str, Any]] = []
        for receipt in ready_receipts:
            receipt_id = receipt.get("receipt_id")
            confirmation_item = confirmations.get(str(receipt_id))
            if not confirmation_item:
                validated.append(
                    self._business_reject(
                        receipt_id,
                        "RECEIPT_NOT_CONFIRMED",
                        "Receipt is absent from the persisted confirmation artifact",
                    )
                )
                continue

            try:
                normalized_amount = self._normalize_amount(receipt.get("monto") or receipt.get("amount"))
                confirmed_amount = self._normalize_amount(
                    confirmation_item.get("confirmed_amount")
                    or confirmation_item.get("monto_confirmado")
                    or confirmation_item.get("amount")
                )
                normalized_category = self._normalize_category(receipt.get("categoria") or receipt.get("category"))
                confirmed_category = self._normalize_category(
                    confirmation_item.get("confirmed_category")
                    or confirmation_item.get("categoria_confirmada")
                    or confirmation_item.get("category")
                )
            except ValueError as exc:
                validated.append(self._business_reject(receipt_id, "INVALID_CONFIRMED_FIELDS", str(exc)))
                continue

            if normalized_amount != confirmed_amount:
                validated.append(
                    self._business_reject(
                        receipt_id,
                        "CONFIRMED_AMOUNT_MISMATCH",
                        "Ready receipt amount does not match confirmed amount",
                    )
                )
                continue
            if normalized_category != confirmed_category:
                validated.append(
                    self._business_reject(
                        receipt_id,
                        "CONFIRMED_CATEGORY_MISMATCH",
                        "Ready receipt category does not match confirmed category",
                    )
                )
                continue
            if not self._category_allowed(normalized_category, allowed_categories):
                validated.append(
                    self._business_reject(
                        receipt_id,
                        "INVALID_CATEGORY",
                        "Confirmed category is outside the explicit contract category set",
                    )
                )
                continue

            try:
                idempotency_key = self._idempotency_key(receipt, normalized_amount, normalized_category)
            except ValueError as exc:
                validated.append(self._business_reject(receipt_id, "IDEMPOTENCY_INPUT_INVALID", str(exc)))
                continue

            validated.append(
                ReceiptValidation(
                    receipt=receipt,
                    normalized_amount=normalized_amount,
                    normalized_category=normalized_category,
                    idempotency_key=idempotency_key,
                )
            )

        return validated

    def _idempotency_key(self, receipt: Dict[str, Any], amount: str, category: str) -> str:
        digest = (
            receipt.get("file_digest")
            or receipt.get("file_sha256")
            or receipt.get("source_file_digest")
            or receipt.get("sha256")
        )
        if not digest:
            raise ValueError("Receipt must include immutable file digest for idempotency")
        basis = {
            "schema": "registrar_boletas_roma.idempotency.v1",
            "receipt_id": str(receipt.get("receipt_id") or ""),
            "file_digest": str(digest).lower(),
            "page_index": receipt.get("page_index"),
            "amount": amount,
            "category": category,
            "date": str(receipt.get("fecha") or receipt.get("date") or ""),
            "supplier": self._normalize_text(receipt.get("proveedor") or receipt.get("supplier") or ""),
            "document_number": self._normalize_text(receipt.get("numero") or receipt.get("document_number") or ""),
        }
        return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _resolve_token(self) -> str:
        if WidgetParamResolver is None:
            raise RuntimeError("WidgetParamResolver is unavailable for ROMA_USER_TOKEN")
        widget_data = (self.orchestration_event.extra_params or {}).get("widget_data", {})
        resolver = WidgetParamResolver(self.orchestration_event)
        resolved = resolver.resolve_positional(widget_data, count=1)
        token = resolved[0] if resolved else None
        if not token:
            raise ValueError("Missing widget secret ROMA_USER_TOKEN")
        return token

    def _resolve_mode(self) -> str:
        mode = os.getenv("POMPEYO_ROMA_WRITE_MODE", DEFAULT_ROMA_WRITE_MODE).strip().lower()
        if mode not in ROMA_WRITE_MODES:
            raise ValueError(
                f"Invalid POMPEYO_ROMA_WRITE_MODE={mode!r}; expected one of {sorted(ROMA_WRITE_MODES)}"
            )
        return mode

    def _is_test_execution(self) -> bool:
        extra_params = self.orchestration_event.extra_params or {}
        return bool(
            extra_params.get("is_test")
            or extra_params.get("is_node_test")
            or extra_params.get("test_execution_uuid")
            or extra_params.get("is_operator_params_test")
            or extra_params.get("dry_run")
        )

    def _with_test_defaults(self, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        if not extra_params.get("is_operator_params_test"):
            return tool_args
        defaults = {
            "node_id": "265926",
            "ready_receipts_uuid": "test-ready-receipts",
            "confirmation_uuid": "test-confirmation",
            "batch_hash": "batch-hash-v1",
            "batch_version": "1",
        }
        merged = dict(defaults)
        merged.update({key: val for key, val in tool_args.items() if val not in (None, "")})
        return merged

    def _build_output(
        self,
        mode: str,
        is_test: bool,
        batch_hash: str,
        batch_version: str,
        results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        summary = {
            "accepted_count": sum(1 for item in results if item["status"] == "accepted"),
            "rejected_business_count": sum(1 for item in results if item["status"] == "rejected_business"),
            "failed_technical_count": sum(1 for item in results if item["status"] == "failed_technical"),
        }
        return {
            "schema_version": "registrar_boletas_roma.results.v1",
            "generated_at_epoch": int(time.time()),
            "mode": "test" if is_test else mode,
            "wrote_to_roma": mode == "live" and not is_test and summary["accepted_count"] > 0,
            "batch_hash": batch_hash,
            "batch_version": batch_version,
            "summary": summary,
            "results": results,
            "audit": {
                "auth": "redacted",
                "roma_base_url_configured": bool(os.getenv("POMPEYO_ROMA_BASE_URL")),
                "production_endpoint_mapping": "unresolved",
            },
        }

    def _no_write_result(self, item: ReceiptValidation, mode: str, is_test: bool) -> Dict[str, Any]:
        reason = "test_no_write" if is_test else f"{mode}_no_write"
        return {
            "receipt_id": item.receipt["receipt_id"],
            "status": "accepted",
            "roma_record_id": None,
            "idempotency_key": item.idempotency_key,
            "no_write": True,
            "no_write_reason": reason,
            "payload_echo_min": self._payload_echo_min(item.receipt, item.idempotency_key),
        }

    def _payload_echo_min(self, receipt: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        return {
            "receipt_id": receipt.get("receipt_id"),
            "file_uuid": receipt.get("file_uuid"),
            "amount": receipt.get("monto") or receipt.get("amount"),
            "category": receipt.get("categoria") or receipt.get("category"),
            "idempotency_key": idempotency_key,
        }

    def _business_reject(self, receipt_id: Any, code: str, message: str) -> Dict[str, Any]:
        return {
            "receipt_id": receipt_id,
            "status": "rejected_business",
            "error_code": code,
            "error_message": message,
        }

    def _technical_failure(self, receipt_id: Any, idempotency_key: str, code: str, message: str) -> Dict[str, Any]:
        return {
            "receipt_id": receipt_id,
            "status": "failed_technical",
            "error_code": code,
            "error_message": message,
            "idempotency_key": idempotency_key,
        }

    def _extract_receipts(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        receipts = payload.get("ready_receipts") or payload.get("receipts") or payload.get("items")
        if not isinstance(receipts, list):
            raise ValueError("ready_receipts file must contain ready_receipts, receipts, or items array")
        return receipts

    def _confirmation_map(self, confirmation: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        items = (
            confirmation.get("confirmed_receipts")
            or confirmation.get("receipts")
            or confirmation.get("items")
            or []
        )
        return {str(item.get("receipt_id")): item for item in items if item.get("receipt_id") is not None}

    def _allowed_categories(self, confirmation: Dict[str, Any]) -> Optional[set[str]]:
        raw = confirmation.get("allowed_categories")
        if raw is None:
            return None
        return {self._normalize_category(item) for item in raw}

    def _category_allowed(self, category: str, allowed_categories: Optional[set[str]]) -> bool:
        if not CATEGORY_RE.match(category):
            return False
        return allowed_categories is None or category in allowed_categories

    def _normalize_amount(self, value: Any) -> str:
        if value in (None, ""):
            raise ValueError("Confirmed amount is required")
        text = str(value).strip().replace("$", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            return str(Decimal(text).quantize(Decimal("1")))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid amount: {value!r}") from exc

    def _normalize_category(self, value: Any) -> str:
        if value in (None, ""):
            raise ValueError("Confirmed category is required")
        return self._normalize_text(value).upper().replace(" ", "_")

    def _normalize_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value).strip())

    def _short_hash(self, value: Any) -> str:
        text = str(value)
        return f"{text[:8]}..." if len(text) > 8 else text

    def _require_arg(self, tool_args: Dict[str, Any], name: str) -> Any:
        value = tool_args.get(name)
        if value in (None, ""):
            raise ValueError(f"Missing required parameter: {name}")
        return value

    def _extract_tool_args(self) -> Dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])
        if not tool_calls:
            logger.warning("No tool calls found in orchestration event")
            return {}
        return tool_calls[0].get("args", {})
