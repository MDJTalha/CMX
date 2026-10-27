# Phase 1 — Validation and persistence

## Scope and status

This phase has two distinct gates:

1. **Engineering gate** — versioned assessment templates, durable tenant-scoped snapshots, claim-level evidence provenance/freshness, schema versioning, and backup/restore tests.
2. **Market-validation gate** — real customer interviews and paid-pilot evidence for one specific SME workflow.

The engineering gate is implemented in this branch and must pass GitHub Actions before it is considered verified. The market-validation gate cannot be completed by code or inferred from hypothetical templates. CMX must not describe a workflow or niche as validated until interview notes and pilot evidence are supplied by real prospective customers.

## Durable assessment records

The SQLiteAssessmentStore stores a request/response snapshot with:
- a server-generated immutable assessment identifier;
- tenant scope derived from the authenticated principal, never from request JSON;
- exact template ID and version;
- UTC creation time and a canonical SHA-256 snapshot digest;
- bounded record sizes and bounded listing;
- integrity verification when records are read;
- fail-closed behavior on a digest mismatch.

The API persists successful evaluation results in CMX_ASSESSMENT_DB_PATH (default data/cmx-assessments.sqlite3). The response carries X-CMX-Assessment-ID. GET /api/v1/assessments and GET /api/v1/assessments/{assessment_id} require assessment:read. The store filters every read by the authenticated tenant. A missing or cross-tenant record returns the same 404 response.

The snapshot digest detects accidental or unauthorized row changes when read; it is not a signature against an attacker who can rewrite both the row and digest. Production deployment requires restrictive database access, backups, and a stronger external audit trust boundary if a privileged database attacker is in scope.

## Versioned templates

src/cmx/assessment_templates.py contains versioned discovery templates. An assessment references a specific template ID/version; unknown versions are not silently upgraded. Templates are explicitly marked not_validated and are starting points, not evidence of market demand or a promise that the listed requirements are appropriate for every customer.

## Claim-level evidence

Evidence claims record the claim, source title/URL/owner, retrieval time, optional expiry, verification method, and SHA-256 of the captured content. Expiry timestamps must be timezone-aware and later than retrieval. Reads expose current or expired based on the supplied/current time. Source URLs and labels are provenance fields, not independent proof that a source is trustworthy. Ingestion, URL fetching, claim extraction, and independent source verification are not included in this increment.

## Migration and backup/restore

The store maintains a schema migration version table and rejects a database created by a newer incompatible schema. Backup uses SQLite's online backup API to include committed state safely. Restore validates the backup's SQLite integrity and schema version, restores through a temporary file, validates the restored database, and only then atomically replaces the destination.

Backups should be stored separately from the primary database, protected with least privilege, and periodically restore-tested in the intended deployment environment.

## Automated tests

The assessment store, template, and API tests cover:
- persistence across store/process reinitialization;
- tenant isolation and duplicate IDs;
- snapshot tampering detection;
- claim provenance, content digest, expiry, and invalid timestamps;
- backup/restore of records and evidence;
- schema/template version behavior;
- authenticated assessment history, read permission, and cross-tenant non-disclosure.

GitHub Actions results must be checked on the final head commit before merge. No claim is made here that the branch has passed CI until the run reports success.

## Customer validation — owner input required

Please complete this with actual discovery evidence. Do not add invented names, quotes, budgets, or willingness-to-pay statements to the repository.

For at least 5 interviews in one narrow SME workflow:
1. Record role (or anonymized role code), business size band, date, and consent to retain notes.
2. Capture the current process, frequency, volume, measured cost/time/error baseline, and consequences of failure.
3. Record existing workarounds and tools already tried.
4. Identify the economic buyer, approval path, data/security constraints, and procurement timing.
5. Ask whether the customer would run a paid pilot; record the exact proposed scope, price, success metric, and their actual response.
6. Separate verbatim statements from interpretation. Remove personal or confidential information not needed for the decision.
7. Summarize evidence for and against the hypothesis and explicitly record whether it is validated, rejected, or still uncertain.

A suggested minimum decision gate is five interviews in the same workflow plus at least one written paid-pilot commitment or a clearly documented reason to revise the hypothesis. This is a project decision rule, not a universal market-research standard. Do not mark the phase fully complete without real evidence.

## Known limitations

- SQLite is a single-node reference database, not a distributed database.
- Assessment snapshots are append-only through the public API, but a privileged database owner can modify the file.
- Evidence content is hashed but not automatically fetched or independently verified.
- No customer interviews or paid pilot can be performed on behalf of the owner by this code change.
- A backup/restore test in CI does not replace a scheduled backup and restore drill in production.


## Current market-validation state and next action

As of 2026-10-27, owner-provided status is: workflow category = SME back-office workflow automation; interviews = not started; paid-pilot status = not established; additional evidence = none provided. Therefore the market-validation gate remains **not started / not established**. No demand, willingness-to-pay, customer, or savings claims should be inferred.

The evidence-first research kit is in [phase1-market-validation-kit.md](phase1-market-validation-kit.md), with an empty [market-validation-register.csv](templates/market-validation-register.csv). It contains a neutral interview script, recruitment guidance, an evidence-quality rubric, outreach drafts, and explicit decision criteria. These are process assets, not customer evidence.

Next actions:
1. Identify real SME operators responsible for recurring back-office processes and invite them to voluntary discovery interviews.
2. Conduct and record the interviews using the guide; mark unknown values as unknown and capture negative/disconfirming evidence.
3. Select a narrow workflow only after comparing the real evidence; do not preselect a winner based on the template.
4. Test paid-pilot willingness with a bounded scope and measurable success criterion. Count only an explicit written commitment as commitment evidence.
5. Update the register and decision record with anonymized, consented summaries. Do not put personal contact details or confidential source material in this public repository.

The software work can prepare the research and store the resulting assessment data, but it cannot conduct or truthfully substitute for customer interviews. Phase 1 must remain open until the market gate and production restore drill are evidenced.
