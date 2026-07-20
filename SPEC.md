# SPEC - RegistrarBoletasRomaFn

## Purpose

Pompeyo-owned ROMA receipt writer for pipeline 81353. This function is the only
receipt-writing boundary, but it writes only when every receipt is backed by a
persisted confirmation artifact.

## Required Inputs

- `ready_receipts_uuid`: session file containing `ready_receipts`/`receipts`/`items`.
- `confirmation_artifact_uuid`: session file containing confirmed receipt decisions.
- `batch_hash`: exact immutable hash for the batch.
- `batch_version`: exact contract version for the batch.
- `node_id`: required so Chask injects widget params.

Each ready receipt must include:

```json
{
  "receipt_id": "r-001",
  "file_uuid": "session-file-uuid",
  "file_digest": "sha256-of-immutable-file-or-page-bytes",
  "page_index": 0,
  "fecha": "2026-07-20",
  "monto": 12990,
  "categoria": "COMBUSTIBLE",
  "proveedor": "Proveedor",
  "numero": "123"
}
```

The corrected analyzer contract may instead provide:

```json
{
  "receipt_id": "receipt-001",
  "source": {
    "file_uuid": "file-receipt-001",
    "source_content_sha256": "sha256:...",
    "page_index": 0,
    "page_number": 1
  },
  "proposed_amount": {
    "numeric_value": 45000,
    "currency": "CLP"
  },
  "expense_category": {
    "id": "roma-cat-101",
    "name": "Combustible",
    "status": "resolved",
    "ambiguous": true,
    "candidates": [
      {"id": "roma-cat-101", "name": "Combustible"},
      {"id": "roma-cat-303", "name": "Viajes"}
    ]
  }
}
```

`proposed_amount.numeric_value`, `expense_category.id/name`, and
`source.source_content_sha256` are first-class inputs. Nested category objects
must never be stringified. If the analyzer category is ambiguous, the writer
accepts it only when the persisted confirmation pins one of the analyzer
catalog candidates.

The confirmation artifact must include:

```json
{
  "schema_version": "1",
  "batch_hash": "exact-batch-hash",
  "batch_version": "1",
  "allowed_categories": ["COMBUSTIBLE", "PEAJE"],
  "confirmed_receipts": [
    {
      "receipt_id": "r-001",
      "confirmed_amount": 12990,
      "confirmed_category": "COMBUSTIBLE",
      "confirmed_category_id": "COMBUSTIBLE"
    }
  ]
}
```

`confirmed_category_id` from the persisted confirmation is the authoritative
ROMA mapping. `category_id` remains accepted as a backwards-compatible alias.
`confirmed_category`/`category_name` is retained for audit and must match the
selected analyzer candidate name. Reject if the category is unresolved/missing,
the confirmation lacks a category ID, or the confirmed ID/name is absent from
the analyzer candidates.

## Write Modes

- `disabled` (default): validate, produce idempotency keys and result file, do not call ROMA.
- `shadow`: validate and produce no-write accepted results, do not call ROMA.
- `live`: resolve `ROMA_USER_TOKEN` at runtime and call the injected ROMA adapter, except in any Chask test execution.

All tests and `extra_params.is_test`/`is_node_test`/`is_operator_params_test`/`test_execution_uuid`
force no-write behavior regardless of mode.

## Idempotency

The idempotency key is SHA-256 over:

- immutable file digest,
- `page_index`,
- `page_number`,
- normalized amount,
- confirmed category ID and name,
- date,
- supplier,
- document number,
- `receipt_id`,
- internal schema marker.

Receipts missing an immutable digest are rejected as `rejected_business`.

## ROMA Endpoint Mapping

Local repository context does not prove the exact ROMA receipt endpoint or
category schema. Production HTTP submission is therefore isolated behind
`PompeyoRomaHttpAdapter`. The default adapter has `endpoint_path=None`; in live
mode it returns `failed_technical` with `ROMA_ENDPOINT_UNRESOLVED` rather than
guessing or calling production.

Before enabling live writes, a reviewer must provide the endpoint path and
category schema from a local/proven ROMA contract and add tests for that mapping.

## Result File

The function uploads `roma_submission_results.json`:

```json
{
  "schema_version": "registrar_boletas_roma.results.v1",
  "mode": "disabled",
  "wrote_to_roma": false,
  "batch_hash": "exact-batch-hash",
  "batch_version": "1",
  "summary": {
    "accepted_count": 1,
    "rejected_business_count": 0,
    "failed_technical_count": 0
  },
  "results": [
    {
      "receipt_id": "r-001",
      "status": "accepted",
      "idempotency_key": "..."
    }
  ],
  "audit": {
    "auth": "redacted",
    "roma_base_url_configured": true,
    "production_endpoint_mapping": "unresolved"
  }
}
```
