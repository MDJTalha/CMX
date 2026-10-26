# Isolated JSON Handler Execution

## What it does

SubprocessJSONHandler is an opt-in adapter for a handler explicitly registered by trusted application code. It executes a fixed absolute executable and fixed argument vector with shell execution disabled. A workflow plan can select only a registered capability ID; it cannot supply the command, arguments, working directory or environment.

The child receives one JSON document on standard input with payload, dependency outputs and the non-secret execution context. It must emit one JSON value on standard output. The adapter bounds serialized input (default 1 MiB), stdout (default 256 KiB) and stderr (default 16 KiB). A stream exceeding its cap causes process termination. The handler timeout budget is enforced by the parent; timeout produces the safe error code deadline_exceeded_outcome_unknown.

## Configuration

Configure the adapter in trusted application code, never from plan/request JSON. Use an absolute executable path and provide only the environment variables the child needs. Environment inheritance is disabled unless an explicit mapping is supplied. Prefer isolated interpreter flags such as -I for trusted Python adapter modules. Do not place secrets in command-line arguments because process listings may reveal them.

## Timeout and side effects

Terminating the child bounds the parent-visible execution time, but does not prove that an external service did not process a request. A child may start a side effect before the deadline, or a descendant process may outlive its parent on some platforms. On timeout, treat the outcome as unknown, reconcile against the external system using a stable idempotency key, and do not automatically replay the operation. Production Windows deployments should use a Job Object or dedicated worker/container supervisor to ensure descendant processes are also terminated.

## Security limits

This is not a security sandbox. A trusted executable runs with the operating-system identity and permissions of the CMX process and can access resources permitted to that identity. Do not execute untrusted binaries, model-generated code, arbitrary plan commands or user-uploaded scripts. For untrusted or high-impact handlers, use a separately permissioned worker/container, filesystem/network restrictions, CPU/memory quotas and external monitoring. This adapter is not connected to public API routes by default; it is available for explicit handler registration only.
