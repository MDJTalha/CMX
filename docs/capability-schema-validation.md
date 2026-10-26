# Capability JSON Schema Validation

## Scope

CMX validates declared capability schemas and checks plan-step input and capability output payloads against those contracts. The implementation uses JSON Schema Draft 2020-12 through the `jsonschema` package.

## Safety boundaries

- Schemas cannot resolve remote `$ref`, `$dynamicRef` or `$recursiveRef` resources. Local fragment references are allowed.
- Schema documents are limited to 32 KiB and 32 container levels.
- Payloads are limited to 256 KiB and 64 container levels.
- Validation errors are capped and report paths/constraint names rather than echoing rejected values.
- Plan validation checks input payloads before execution; it does not execute the plan.
- Output validation is an explicit registry operation; no executor currently calls it automatically.
- Permission sets remain caller-provided. They are not a trusted identity or production authorization mechanism.

## API within the Python package

- `validate_json_schema(schema)` verifies schema shape and boundedness.
- `validate_json_payload(schema, payload)` validates a JSON value against its schema.
- `CapabilityRegistry.validate_input(..., granted_permissions=...)` authorizes a declared capability and validates input.
- `CapabilityRegistry.validate_output(..., granted_permissions=...)` authorizes a declared capability and validates output.
- `CapabilityRegistry.validate_plan(..., granted_permissions=...)` validates each step's capability, input, permission requirements, approval policy and retry allowance without executing anything.

## Next work

The next kernel increment should establish trusted identity-bound permission derivation, durable task and audit persistence, and a carefully constrained executor interface with timeout, idempotency and cancellation semantics. Production execution must remain disabled until those controls have been reviewed and integration-tested.
