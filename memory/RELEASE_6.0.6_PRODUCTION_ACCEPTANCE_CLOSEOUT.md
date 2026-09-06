# Release 6.0.6 Production Acceptance & UAT Closeout Report

**Closeout date:** 2026-09-01 (WAT)  
**Repository:** `michaelheavensd1st/Agrovix`  
**Release branch:** `develop`  
**Current accepted production SHA:** `66bac60667df190c4cc2f704ed3d572d8828c90f`
**Alembic head:** `0015_aqua_transfer_integrity`

**Production runtime verification:** 2026-09-05 (UTC)
**Documentation closeout:** PR #39 pending review

## 1. Decision

**PRODUCTION ACCEPTED / FUNCTIONAL UAT CLOSED.**

Release 6.0.6 passed the functional UAT and production-acceptance gates described below. The initial 2026-09-01 acceptance used Git SHA `48c236ac2e625f0ca18c0e7e7f9940327c2197e4`. Production runtime verification now passes on the PR #41 source commit `66bac60667df190c4cc2f704ed3d572d8828c90f` after the receipt-fixture dashboard quarantine was merged, the fixture was operationally retired, and the canonical production frontend was rebuilt with its Production-only API proxy target corrected.

Production acceptance and quarantine runtime verification are complete and PASS. Documentation closeout in PR #39 remains pending review; this report does not resolve or represent resolution of any PR #39 review thread. Production frontend validation is established as recorded in Section 8. This acceptance does not waive the remaining non-blocking technical debt recorded in Section 9.

## 2. Canonical release baseline

At initial acceptance on 2026-09-01 the repository baseline was verified as:

- Branch: `develop`
- Local HEAD: `48c236ac2e625f0ca18c0e7e7f9940327c2197e4`
- `origin/develop`: `48c236ac2e625f0ca18c0e7e7f9940327c2197e4`
- Divergence: `0 / 0`
- Worktree: clean
- Alembic: exactly one head, `0015_aqua_transfer_integrity`

Recent remediation lineage included:

- PR #36 — `fix(production): return conflict for duplicate unit codes`
- PR #37 — `fix(receipts): scope warehouse lookup to purchase order`
- PR #38 — `fix(web): expose purchase orders from organization hub`
- PR #38 merge commit was the initial accepted release SHA.

The canonical accepted and deployed production baseline subsequently became:

- Branch: `develop`
- Git SHA: `66bac60667df190c4cc2f704ed3d572d8828c90f`
- Lineage: accepted PR #41 source — `fix(inventory): enforce receipt fixture quarantine`
- Railway production deployment: `SUCCESS` with a `RUNNING` instance
- Canonical Vercel production frontend: `https://agrovix-web.vercel.app`
- Alembic: exactly one head, `0015_aqua_transfer_integrity`

## 3. UAT remediation and acceptance evidence

### Aquaculture transfer integrity and timeline

PASS.

Validated atomic transfer behavior, source/destination population projections, idempotent replay, conflicting replay rejection, overdraw rejection, cross-farm destination protection, and sanitized timeline rendering without destination UUID exposure.

### Production-unit duplicate-code handling

PASS / REMEDIATED.

Duplicate production-unit code creation now returns sanitized HTTP `409` with code `production_unit_code_conflict` instead of leaking an unhandled SQLAlchemy integrity failure as HTTP `500`.

### Purchase receipt warehouse lookup

PASS / REMEDIATED.

Frontend warehouse discovery was corrected to scope the request by Purchase Order ID rather than organization ID. Browser UAT confirmed the prior repeated `404` behavior was removed.

### Transactional email verification

PASS / REMEDIATED.

A verified sending subdomain was configured and a fresh registration email was delivered successfully. The verification link completed successfully without weakening verification requirements.

### Purchase Order navigation

PASS / REMEDIATED.

The organization workspace now exposes an Operations → Purchase Orders navigation card. Browser UAT confirmed the organization-scoped Purchase Orders list loads correctly.

### Purchase Order lifecycle

PASS.

Validated submit, withdraw, resubmit, independent approval, creator self-approval prevention, reject, revise, cancel, reason-length validation, invalid post-terminal transitions, and mutation replay behavior.

### Purchase Order draft edit / optimistic concurrency

PASS.

Fixture: `PO-2026-000004`.

- Normal Draft edit persisted `supplier_reference = UAT-EDIT-V2`.
- Version advanced from `v1` to `v2`.
- A stale PATCH using `expected_version: 1` against current version `2` returned HTTP `409` with code `purchase_order_version_conflict` and context `current_version: 2`.
- Final read proved the rejected stale write changed nothing: status remained `DRAFT`, version remained `2`, and supplier reference remained `UAT-EDIT-V2`.

### Purchase receipt posting / inventory posting

PASS.

A controlled production-acceptance fixture was created against the Railway production API to validate the receiving flow end to end at API level.

Controlled Purchase Order:

- Purchase Order ID: `6a3a1d6e-8065-4ff7-b2ee-e7fc596b10b4`
- Initial receivable state: `APPROVED`, version `3`
- Purchase Order line ID: `e4e41ad5-3e24-464e-ab7d-77031758966a`
- Ordered quantity: `100.000000 kg`
- Eligible warehouse ID: `94e15351-d4b4-46bc-ac36-a304c675ba8f`
- Warehouse code: `UAT_RECEIPT_WH_A`

Independent approval was performed by a separate non-superuser organization-scoped approver account. Creator self-approval was therefore not used.

First partial receipt:

- HTTP `201`
- Receipt ID: `27c2131f-cc97-404f-bc6b-cfe8753357e8`
- GRN: `GRN-2026-000001`
- Quantity: `40.000000 kg`
- Inventory lot ID: `958208f1-dd3b-41cd-b860-51f43a3313ef`
- Inventory transaction ID: `42462b62-b99e-4f3e-abf1-f6a5aba8902f`
- Purchase Order transitioned from `APPROVED v3` to `PARTIALLY_RECEIVED v4`
- Readback confirmed `40.000000 / 100.000000 kg` received.

Idempotency validation:

- Replaying the exact first receipt with the same `Idempotency-Key` returned HTTP `200`.
- Response header `x-idempotent-replay: true` was present.
- The replay returned the same receipt ID, GRN, inventory lot ID, and inventory transaction ID.
- Reusing the same idempotency key with a changed quantity returned HTTP `409` with code `idempotency_key_payload_conflict`.
- Readback after the rejected changed-payload request confirmed the Purchase Order remained `PARTIALLY_RECEIVED v4` with exactly `40.000000 kg` received.

Final receipt:

- HTTP `201`
- Receipt ID: `f36cd2c2-25b2-4bb1-83be-e989560401a9`
- GRN: `GRN-2026-000002`
- Quantity: `60.000000 kg`
- Inventory lot ID: `7d91e5a2-d289-4e9a-9ab7-35763751891f`
- Inventory transaction ID: `94f0aba1-845b-48c8-aa80-18be9209aa9e`
- Purchase Order transitioned from `PARTIALLY_RECEIVED v4` to `RECEIVED v5`
- Final readback confirmed `100.000000 / 100.000000 kg` received and canonical quantities matched exactly.

Post-completion protection:

- A further receipt attempt against the fully received Purchase Order returned HTTP `409` with code `purchase_order_not_receivable`.
- No additional receipt mutation was accepted after the Purchase Order reached `RECEIVED`.

This closes the API-level Purchase Receipt posting gate, including inventory-lot creation, inventory-transaction creation, partial and full receipt transitions, replay safety, conflicting replay rejection, and terminal over-receipt protection.

### Production receipt-fixture inventory isolation

PASS.

Because Purchase Receipt posting creates immutable inventory-ledger history, a read-only production database audit was performed before authorizing fixture retirement.

The controlled receipt fixture is logically isolated from all other observed inventory activity:

- Warehouse `94e15351-d4b4-46bc-ac36-a304c675ba8f` is explicitly named `UAT Receipt Warehouse A` with code `UAT_RECEIPT_WH_A`.
- Inventory item `3ee95b24-b3c4-4d56-9dcc-141ccd755f84` is explicitly named `UAT Receipt Feed` with code `UAT-RECEIPT-FEED`.
- The UAT warehouse contains exactly `2` inventory transactions, `2` active lots, and `1` distinct inventory item.
- The two transactions total exactly `100.000000 kg`, matching the controlled `40 kg` and `60 kg` Purchase Receipt scenarios.
- The UAT inventory item has no transaction activity or active lots in any other warehouse.
- The only transaction type recorded for the UAT item is `receipt`.
- Read-only schema discovery returned `inventory_items`, `inventory_lots`, and `inventory_transactions` as the inventory-related tables relevant to this fixture; no separately named stock/balance table was identified.
- Receipt transaction IDs are `42462b62-b99e-4f3e-abf1-f6a5aba8902f` and `94f0aba1-845b-48c8-aa80-18be9209aa9e`.
- Receipt lot IDs are `958208f1-dd3b-41cd-b860-51f43a3313ef` and `7d91e5a2-d289-4e9a-9ab7-35763751891f`.

Accordingly, the synthetic `100 kg` balance is quarantined inside a dedicated, clearly identified UAT warehouse/item ledger namespace and is not commingled with another observed warehouse or inventory-item ledger. The immutable Purchase Receipt and inventory transaction history must not be deleted, rewritten, or offset merely for UAT cleanup. No compensating inventory adjustment is authorized or required for this closeout because the fixture remains quarantined as acceptance evidence and is not being converted into operational stock.

#### Operational retirement and preservation verification — 2026-09-04

After a final `REPEATABLE READ`, `READ ONLY` preflight ended with `ROLLBACK`, the fixture was retired only through supported, audited application lifecycle operations:

- Warehouse `94e15351-d4b4-46bc-ac36-a304c675ba8f` / `UAT_RECEIPT_WH_A`: `status = closed`; `deleted_at IS NULL`.
- Inventory item `3ee95b24-b3c4-4d56-9dcc-141ccd755f84` / `UAT-RECEIPT-FEED`: `is_active = false`; `deleted_at IS NULL`.
- Supplier `8f09ddc1-9597-4b4c-a43a-19789c54ba77` / `UAT-RECEIPT-SUPPLIER`: inactive/deactivated and not deleted. Its deactivation timestamp is populated and its reason is `Release 6.0.6 production acceptance fixture retired after verified isolation audit`.

Final read-only verification confirmed that operational retirement did not delete, rewrite, or offset acceptance evidence:

- Purchase Order `6a3a1d6e-8065-4ff7-b2ee-e7fc596b10b4` remains `RECEIVED` and retains its supplier relationship.
- The chain contains exactly `1` PO line, `2` Purchase Receipts, `2` receipt lines, `2` inventory lots, `2` receipt ledger transactions, and `5` PO transitions.
- Historical receipt quantity remains exactly `100.000000 kg`; the two lot balances remain exactly `40.000000 kg` and `60.000000 kg`.
- No compensating adjustment was created.
- Isolation re-verification found no transfers, issues, adjustments, reversals, consumption, production-event references, new fixture inventory activity, cross-warehouse/item contamination, or unexpected fixture dependencies.

The six original fixture audit events remain present, and their recorded integrity digests were verified unchanged:

- `0f91c3eb-6fb6-414d-8ab2-ac0ebdff26f8`
- `2cf3145c-aec8-4bf8-8e38-a240acaf8e8a`
- `39037252-7e56-4939-8945-2288fd4fe78f`
- `366a809a-de29-4e84-a1e5-6c689cd593a9`
- `73bdc136-02f5-4033-9c0b-7d1f885f18c8`
- `a70e30eb-919e-4afe-85f7-7365121f4f50`

Normal warehouse-retirement, item-retirement, and supplier-deactivation audit events were created. No prior audit event was deleted or rewritten.

The exact dashboard quarantine for warehouse UUID `94e15351-d4b4-46bc-ac36-a304c675ba8f` remains required and present in the deployed source. Closing the warehouse does not make the quarantine obsolete: the dashboard projection can otherwise include closed warehouses and historical lots, while the intentionally preserved fixture ledger still contains the synthetic `100 kg` balance. The quarantine must remain until that projection contract changes through separately reviewed application work.

#### Authenticated production API and dashboard projection verification — 2026-09-05

Authenticated requests to `https://api-staging.aegisfarm.com` established the administrative-record and operational-projection boundary at runtime:

- Administrative warehouses returned HTTP `200`; `UAT_RECEIPT_WH_A` remained present with `status = closed`.
- Warehouses requested with `operational_only=true` returned HTTP `200`; `UAT_RECEIPT_WH_A` was absent.
- Administrative inventory items returned HTTP `200`; `UAT-RECEIPT-FEED` remained present with `is_active = false`.
- Inventory items requested with `operational_only=true` returned HTTP `200`; `UAT-RECEIPT-FEED` was absent.

This proves the historical administrative records remain preserved while the retired fixture is excluded from operational API projections.

### Isolated release-UAT deployed-browser receipt idempotency and recovery

PASS.

The previously outstanding browser-level receipt idempotency/recovery behavior was subsequently validated against the exact deployed frontend build at Git SHA `66bac60667df190c4cc2f704ed3d572d8828c90f`, using the Vercel deployment `https://agrovix-cqkncdkmc-commander-dons-projects.vercel.app` connected through `/api-proxy` to the isolated Railway release-UAT API at `https://agrovix-api-amused-ambition-release-uat.up.railway.app`.

This browser evidence is intentionally distinct from the production API acceptance fixture above. The browser test ran against the isolated release-UAT environment, not against the production database or a separately established production frontend/domain. Release-UAT Alembic state was independently verified as exactly `0015_aqua_transfer_integrity (head)` for both `alembic current` and `alembic heads`. Runtime `/version` reported the staging environment and `git_commit = unknown`; the frontend SHA attribution therefore comes from deployment metadata rather than the runtime version payload.

Controlled isolated browser fixture:

- Organization ID: `d2f6442a-b525-40cd-b640-8e7c1a261039`
- Organization retained active after testing.
- Purchase Order ID: `39803bb3-a8aa-4688-a96a-c3273088b5d7`
- Purchase Order number: `PO-2026-000001`
- Purchase Order line ID: `59a670c7-33eb-42f4-9042-7820208be6b2`
- Ordered quantity: `2.000000 kg`
- Separate creator/admin and organization-scoped non-superuser approver actors were used.
- Pre-mutation state: `APPROVED`, `0.000000 kg` received, zero receipts, and zero matching test lots.
- Test lot code: `R606-BROWSER-IDEMPOTENCY-001`
- Service worker count: `0`

Deterministic browser idempotency/recovery proof:

- Browser receipt POST #1 used idempotency-key SHA-256 `8c3af9338a0dd8c52418544f27467dae2b36d4bcca8aeefb2767c7f96dfbfbb9`.
- Browser receipt POST #1 request-body SHA-256 was `9500c8a8f75f01ae6cdd8bb7e91672726d00465673eebe97e3903eb7af33837d`.
- The exact browser request was forwarded upstream once and returned HTTP `201`.
- Receipt ID: `15a240bb-99fb-4c00-9882-590525988178`
- GRN: `GRN-2026-000001`
- Inventory lot ID: `78577061-f84e-4240-be5e-eb89069ed507`
- Inventory transaction ID: `576381ce-0210-4006-a64a-cf7807a3a176`
- After the upstream commit, the page-facing response was deliberately aborted to reproduce an ambiguous network result.
- The browser displayed the application's uncertain-result state and exposed `Retry same receipt`.
- Independent read-only verification before retry proved exactly one matching receipt, one matching lot, one matching inventory transaction, and a `1.000000 kg` lot balance.
- Browser receipt POST #2 used exactly the same idempotency-key SHA-256 and exactly the same request-body SHA-256.
- Retry returned HTTP `200` with `X-Idempotent-Replay: true`.
- The browser displayed `This receipt was already recorded. Current data has been refreshed.`
- Final verification again proved exactly one receipt, one lot, one transaction, one total transaction for the lot, and a `1.000000 kg` balance.
- Final Purchase Order state: `PARTIALLY_RECEIVED`.
- Final Purchase Order line `received_quantity`: `1.000000`.
- Total browser receipt POSTs: `2`.

Result: `BROWSER_RECEIPT_IDEMPOTENCY_UAT=PASS`.

### Deployed-browser GRN and immutable receipt-history proof

PASS.

A subsequent read-only browser verification confirmed:

- Receipt GET returned HTTP `200`.
- `receipt.grn` was exactly `GRN-2026-000001`.
- The exact GRN was visible in Purchase Receipt history.
- The GRN detail view opened successfully.
- The browser displayed `Posted receipts are immutable.`

Result: `FINAL_BROWSER_GRN_RECEIPT_PROOF=PASS`.

### Isolated browser-UAT fixture retirement and preservation

PASS.

After the browser acceptance gate closed, only the disposable operational components of the isolated release-UAT fixture were retired:

- Warehouse `141dee5f-032f-4516-b83e-bb48f6e2fd8f` (`R606_BROWSER_WH`) was changed to status `closed`.
- Inventory item `73476ee4-3d25-4d05-9dcd-41e58596c956` (`R606-BROWSER-FEED`) was changed to `is_active = false`.
- Supplier `db2360a1-e44a-469d-bfa4-c70b0cbd3bf2` (`R606-BROWSER-SUPPLIER`) was deactivated with reason `Release 6.0.6 isolated browser receipt UAT fixture retirement`.
- All three retirement mutations returned HTTP `200`.

Independent post-retirement read-only verification proved:

- The closed warehouse remains present in the complete read set and is excluded when `operational_only=true`.
- The inactive inventory item remains present in the complete read set and is excluded when `operational_only=true`.
- Supplier `deactivated_at` is populated and the deactivation reason is preserved.
- Receipt `15a240bb-99fb-4c00-9882-590525988178` remains present.
- GRN remains `GRN-2026-000001`.
- Inventory lot `78577061-f84e-4240-be5e-eb89069ed507` remains present with balance `1.000000 kg`.
- Inventory transaction `576381ce-0210-4006-a64a-cf7807a3a176` remains present.
- The Purchase Order remains `PARTIALLY_RECEIVED`.
- The Purchase Order line remains at `received_quantity = 1.000000`.
- The organization remains active.
- Receipt, lot, transaction, transition, and audit evidence were not destructively deleted or neutralized.

Results: `RETIREMENT_STATE=PASS`, `IMMUTABLE_RECEIPT_CHAIN=PASS`, and `POST_RETIREMENT_VERIFICATION=PASS`.

## 4. Release-UAT deployment verification

Railway release-UAT API health returned HTTP `200` with Redis-backed rate limiting healthy.

Version endpoint reported the staging environment and `/api/v1` prefix.

The exact accepted SHA had successful Railway and Vercel deployment statuses.

Inside the release-UAT Railway container:

- working directory: `/app`
- `alembic current`: `0015_aqua_transfer_integrity (head)`
- `alembic heads`: `0015_aqua_transfer_integrity (head)`

Browser validation included the earlier Purchase Orders navigation smoke test and the later deterministic deployed-browser receipt idempotency/recovery test. The latter used the exact Vercel frontend build at SHA `66bac60667df190c4cc2f704ed3d572d8828c90f` connected through `/api-proxy` to the isolated Railway release-UAT API. The browser test proved a real first receipt commit, an intentionally ambiguous page-facing result, exact same-request retry, HTTP `200` idempotent replay, browser replay acknowledgement, and no duplicate receipt, lot, or inventory transaction.

## 5. Production preflight and database state

Railway production target:

- Project: `talented-fulfillment`
- Project ID: `9112a2e7-1f4b-4bce-8ddb-5a243c43cf29`
- Environment: `production`
- Environment ID: `15d44ea0-e6f5-4c08-bfeb-474ac38b57ce`
- Application service: `Agrovix`
- Application service ID: `0a247ad0-6129-445c-b259-4c64b3fe714d`
- Current production deployment ID: `2944aba2-bf9b-4e04-b9f0-b9ea944b83c4`
- Deployment status: `SUCCESS`
- Running instance ID: `8b244f17-4b24-43b2-9255-5007a8acd3f2`
- Deployed Git SHA: `66bac60667df190c4cc2f704ed3d572d8828c90f`

Production health verification:

- `/health`: HTTP `200`
- Application environment: `production`
- Redis rate limiter: healthy
- PostgreSQL: online

Inside the production Railway application container:

- working directory: `/app`
- `alembic current`: `0015_aqua_transfer_integrity (head)`
- `alembic heads`: `0015_aqua_transfer_integrity (head)`

Therefore no pending Alembic migration was required for this acceptance event.

Final production database verification on 2026-09-04 reconfirmed:

- `alembic current`: `0015_aqua_transfer_integrity (head)`
- `alembic heads`: `0015_aqua_transfer_integrity (head)`
- no migration was performed;
- the closeout database transaction used `REPEATABLE READ`, was `READ ONLY`, and ended with `ROLLBACK`.

## 6. Backup and rollback evidence

A fresh production PostgreSQL volume backup was created immediately before production acceptance:

- Timestamp: `2026-08-31 23:50` WAT
- Size shown by Railway: approximately `112 MB`
- Environment: `production`
- Service: `Postgres`

An older `2026-08-26 22:33` backup remained available as additional historical coverage.

At closeout:

- Point-in-time recovery was OFF.
- No automatic volume-backup schedule was configured.
- PITR was deliberately not enabled during closeout because enabling it would introduce an unrelated service redeployment.

Historical application rollback anchor from the initial acceptance stage:

- Deployment ID: `19028a58-3e26-4e11-95c4-1c44a142c610`
- Git SHA: `48c236ac2e625f0ca18c0e7e7f9940327c2197e4`
- Git branch: `develop`
- Repository: `michaelheavensd1st/Agrovix`

The production application code remained on the exact UAT-approved SHA. A subsequent configuration-only redeployment was performed to apply the corrected transactional-email sender configuration; in-container verification after that redeployment confirmed `RAILWAY_GIT_COMMIT_SHA=48c236ac2e625f0ca18c0e7e7f9940327c2197e4` and `RAILWAY_GIT_BRANCH=develop`. No application-code change was introduced by that configuration redeployment.

Deployment `19028a58-3e26-4e11-95c4-1c44a142c610` is retained here only as historical rollback evidence; it is no longer the active production deployment. The current active deployment is recorded in Section 5.

## 7. Railway platform stability gate

During preflight the Railway dashboard displayed an incident banner concerning delayed deployment initialization. Promotion was held while the incident was considered active.

Railway's status report subsequently marked the incident **Resolved**, stating that the deployment pipeline had stabilized, the queued-deployment backlog had cleared, and throughput had returned to normal. Only after this status was established was the infrastructure-stability gate treated as passed.

## 8. Frontend production verification

At initial acceptance on 2026-09-01, browser validation used the canonical Vercel `develop` Preview and did not establish a separately identified production frontend/domain. That historical qualification is superseded by the final production verification below.

The canonical production frontend is `https://agrovix-web.vercel.app`. It was rebuilt from the same accepted PR #41 source commit after correcting the Production-only `API_PROXY_TARGET` to `https://api-staging.aegisfarm.com`:

- Project: `agrovix-web`
- Project ID: `prj_SKigjKdu1pdn3AqJmHuRz55ppPZV`
- Git branch: `develop`
- Source commit: `66bac60667df190c4cc2f704ed3d572d8828c90f`
- Production API proxy target: `https://api-staging.aegisfarm.com`

The repaired `/api-proxy` reaches Railway, and authenticated browser login succeeds. Authenticated production Inventory Dashboard verification returned:

- Active items: `0`
- Warehouses: `0`
- Tracked lots: `0`
- Out of stock: `0`
- Needs attention: `0 lots`

These runtime results establish that the closed UAT warehouse, inactive UAT item, two historical receipt lots, and synthetic `100 kg` are excluded from the operational dashboard projection while their historical and administrative records remain preserved. Production acceptance and quarantine runtime verification therefore PASS on the accepted PR #41 source commit.

Slash normalization remains a separate nonblocking finding: `/api-proxy/v1/version` returned HTTP `307` with `Location: http://api-staging.aegisfarm.com/api/v1/version/`. This did not prevent the repaired proxy, authenticated login, or dashboard verification from succeeding, but redirect scheme/forwarded-header handling should be hardened separately.

## 9. Accepted non-blocking technical debt / follow-up

The following items were not treated as release blockers for this closeout:

1. Railway production Postgres has PITR disabled and no automatic backup schedule; production backup policy should be hardened separately.
2. Transactional email provider failures can currently surface as generic HTTP `500`; graceful provider-failure handling should be hardened.
3. A previously observed email-verification test login returned `401`; this was not reproduced as a verified product defect and may have been test-password input error.
4. A timezone-sensitive web test has previously failed outside UTC while passing under `TZ=UTC`; this is test-environment debt.
5. Broad API mypy debt remains outside the focused remediation scope.
6. Repository dependency/security-alert debt should be handled as a dedicated security-maintenance stream rather than silently folded into this closeout.
7. Slash normalization through the production proxy can emit an HTTP `Location` for an HTTPS upstream request; proxy forwarded-header or redirect-scheme handling should be hardened separately.

Receipt-fixture operational retirement was completed on 2026-09-04, and production frontend/runtime verification was completed on 2026-09-05; neither is an outstanding technical-debt item.

PR #39 documentation closeout remains pending review. That review status is separate from the completed production acceptance/quarantine runtime PASS, and no PR #39 review thread is resolved by this report revision.

## 10. Data preservation and cleanup rule

UAT fixtures must not be destructively deleted merely to remove acceptance evidence. Cleanup must remain controlled and limited to genuinely disposable UAT data.

The Release 6.0.6 Purchase Receipt fixture has completed supported operational retirement. Its warehouse is closed, item is inactive, and supplier is deactivated without deleting any of them. Its Purchase Receipt records, inventory lots, inventory transactions, PO transitions, and audit events remain immutable acceptance evidence and were not deleted, rewritten, or neutralized by a compensating adjustment. Its synthetic `100 kg` balance remains quarantined in the dedicated `UAT_RECEIPT_WH_A` / `UAT-RECEIPT-FEED` ledger namespace documented in Section 3.

The separate isolated release-UAT browser fixture has also completed controlled retirement of only its disposable operational components. Warehouse `141dee5f-032f-4516-b83e-bb48f6e2fd8f` is closed, inventory item `73476ee4-3d25-4d05-9dcd-41e58596c956` is inactive, and supplier `db2360a1-e44a-469d-bfa4-c70b0cbd3bf2` is deactivated. Its organization remains active, its Purchase Order remains `PARTIALLY_RECEIVED` with `received_quantity = 1.000000`, and receipt `15a240bb-99fb-4c00-9882-590525988178`, GRN `GRN-2026-000001`, inventory lot `78577061-f84e-4240-be5e-eb89069ed507`, inventory transaction `576381ce-0210-4006-a64a-cf7807a3a176`, transition history, and audit evidence remain preserved.

Aquaculture and other Purchase Order fixtures used to establish the acceptance evidence should remain available until the closeout commit/PR is safely merged and the evidence is no longer dependent on live fixture inspection.

## 11. Final acceptance statement

**RELEASE 6.0.6 PRODUCTION ACCEPTANCE: PASS**

The current canonical production Git SHA is:

`66bac60667df190c4cc2f704ed3d572d8828c90f`

Railway production runs that exact SHA with Alembic aligned at `0015_aqua_transfer_integrity`. The canonical production frontend was rebuilt from the same accepted source commit with the corrected Production-only API proxy target. Authenticated production API and browser verification proves the retired fixture remains available administratively and historically while being excluded from operational API and Inventory Dashboard projections. Production acceptance and quarantine runtime verification are therefore complete and PASS. Separately, the deployed frontend build at the same source SHA passed deterministic Purchase Receipt idempotency/recovery validation against isolated release-UAT, including real first commit, ambiguous-result recovery, exact same-request replay, no duplicate inventory effect, GRN/history visibility, and preserved immutable evidence after controlled fixture retirement.

No additional deployment or migration is required for production acceptance. Documentation/PR #39 closeout remains pending review, independently of the runtime PASS; no review thread has been resolved by this revision. Remaining follow-up is limited to that review and the still-valid technical debt in Section 9.
