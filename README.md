# RegistrarBoletasRomaFn

Writer seguro para registrar boletas confirmadas en ROMA dentro del pipeline
81353 de Pompeyo.

La funcion valida dos archivos persistidos en la sesion:

- `ready_receipts_uuid`: boletas listas, con monto/categoria/digest estable.
- `confirmation_uuid`: confirmacion humana persistida, con el mismo
  `batch_hash` y `batch_version`.

No escribe en ROMA si falta la confirmacion, si el hash/version no coincide, si
el monto o categoria confirmada difiere, si falta digest inmutable para
idempotencia, o si la ejecucion es de test.

## Modos

- `POMPEYO_ROMA_WRITE_MODE=disabled`: default. Valida y genera resultados sin escribir.
- `POMPEYO_ROMA_WRITE_MODE=shadow`: sin escritura, pero produce resultados accepted/no_write.
- `POMPEYO_ROMA_WRITE_MODE=live`: unico modo que intenta escribir y solo fuera de tests.

La autenticacion usa el widget secret `ROMA_USER_TOKEN`, resuelto por
`WidgetParamResolver` usando `node_id`. El token nunca se loguea ni se retorna.

## Estado de integracion ROMA

El endpoint exacto para boletas y el schema de categorias no estan probados por
los archivos locales. Por eso la llamada HTTP esta aislada en
`PompeyoRomaHttpAdapter` y el mapeo productivo queda marcado como unresolved.
En `live`, sin mapeo revisado, cada boleta valida falla tecnicamente con
`ROMA_ENDPOINT_UNRESOLVED`; no se adivina ni se llama a produccion.

Ver [`SPEC.md`](./SPEC.md) para el contrato completo.
