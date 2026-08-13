# Paid-provider authority cutover

This is the reviewed runbook for making one grants-tenant host the only Firecrawl and
ZoomInfo spend authority. It does not authorize a deployment. Every production step
must be performed by `grants-ops-guardian`, as the non-root grants tenant, against an
exact committed revision. Never use admin, root, `sudo`, another tenant, or an
uncommitted working tree.

Local SQLite can serialize callers that share a ledger; it cannot prove that another
machine lacks a copied API credential. The cutover is complete only after the old
vendor credentials are revoked or rotated and the replacement credentials exist on
the sole authority host. Copying `.env` or deleting a local ledger is not a cutover.

## Runtime contract

Paid access defaults disabled. An authorized host needs all of the following:

- `GRANT_PAID_PROVIDER_MODE=authority`;
- an absolute `GRANT_PAID_PROVIDER_AUTHORITY_FILE` owned by the grants tenant, mode
  `0600`, outside the repository/deploy tree, and never symlinked;
- one stable, operator-assigned `account_scope_id` per vendor account;
- absolute private standalone ledger paths for every enabled provider;
- complete credentials plus explicit ceilings and the Firecrawl request rate;
- authority, provider, and account-scope metadata in each ledger matching the private
  authority file exactly.

Every provider call, including raw Firecrawl source-discovery, revalidates that contract
before reservation and HTTP. Runtime never creates an authority file or ledger.
`GRANT_PAID_PROVIDER_MODE=disabled` while
any paid credential is installed is an intentional startup failure, not a fallback.

## 1. Preflight and evidence capture

1. Pin the exact reviewed commit and use the normal backup-first deploy protocol in
   `architectural.md`. Record the production revision, process list, crontab, tenant
   identity, source paths, file ownership/modes, SQLite `integrity_check`, and current
   vendor balances without printing credentials.
2. Inventory every machine or process that has ever held the same Firecrawl API key or
   ZoomInfo client credential, including laptops, exports, listeners, cron jobs, and
   manual tooling. Enumerate every app database containing either legacy ledger.
3. Stop the listener and all old-code/manual paid-provider writers on every identified
   host. Keep them stopped through migration, environment switch, and verified restart.
   `BEGIN IMMEDIATE` fences an open source file during copying; it cannot stop an old
   process from writing after the command returns.
4. Back up each source SQLite main/WAL/SHM set before copying it. Run integrity checks
   against the backups. Move any laptop history to a private, immutable cutover copy on
   the authority host only after its writers are stopped.
5. Revoke the old Firecrawl key and old ZoomInfo client credential set using each
   vendor's administrative control. Rotate replacements and install them only on the
   chosen authority host. Remove the old and replacement values from every
   non-authority environment. Record the revocation/rotation evidence without storing
   a secret value in this repository.

Hard stop if any old writer, unknown credential-bearing host, open/indeterminate spend,
or unenumerated same-account history remains.

## 2. Create the host capability

Choose stable opaque account-scope identifiers; they are identity labels, not secrets.
Preview first, then execute once:

```bash
python -m grant_watch.paid_provider_authority_init \
  --destination /absolute/private/paid-provider-authority.json \
  --scope firecrawl=reviewed-firecrawl-account-scope \
  --scope zoominfo=reviewed-zoominfo-account-scope

python -m grant_watch.paid_provider_authority_init \
  --destination /absolute/private/paid-provider-authority.json \
  --scope firecrawl=reviewed-firecrawl-account-scope \
  --scope zoominfo=reviewed-zoominfo-account-scope \
  --execute
```

The command is no-replace and writes mode `0600`. Record its generated `authority_id`
in the private deployment record. Do not copy this capability to another host.

## 3. Merge every Firecrawl history

Set the authority mode/file in the stopped process environment. Preview every explicit
legacy source together; do not initialize an empty ledger merely because one database
looks empty:

```bash
python -m grant_watch.firecrawl_ledger_migration \
  --source /absolute/production-app.db \
  --source /absolute/laptop-cutover-copy.db \
  --destination /absolute/private/firecrawl-runtime-ledger.db
```

If source ceilings conflict, supply one separately reviewed
`--approved-monthly-limit`. The merged history must fit that ceiling; the command never
raises a cap to fit prior calls. A legacy `NULL` request hash in `in_flight` or
`indeterminate` state intentionally opens an account-wide reconciliation circuit.
Resolve that with the vendor/operator before enabling runtime.

With all old writers still stopped, rerun the exact source list with `--execute`.
Execution locks sources in deterministic path order, builds a private sibling file,
verifies exact attempts/counters/backoff plus SQLite integrity, fsyncs, and publishes
without replacement.

## 4. Merge every ZoomInfo history

Preview the full same-account set with stable source scopes, then execute the identical
command with `--execute`:

```bash
python -m grant_watch.zoominfo_ledger_migration \
  --source production=/absolute/production-app.db \
  --source laptop=/absolute/laptop-cutover-copy.db \
  --destination /absolute/private/zoominfo-credit-ledger.db
```

As observed read-only on 2026-08-12, production contains 7 settled spends / 14 credits
of 1,000 and the known laptop history contains 2 settled spends / 3 credits. Unless
vendor reconciliation identifies an exact clone or newer spend, the combined result is
9 spends / 17 credits. The tool deduplicates only exact cloned rows, preserves distinct
same-key charges by source scope, and refuses unsettled state, ID conflicts, limit
mismatch, billed-over-reserved rows, aggregate overdraw, or destination divergence.

## 5. Configure, validate, and start

Configure without printing the values:

```text
GRANT_PAID_PROVIDER_MODE=authority
GRANT_PAID_PROVIDER_AUTHORITY_FILE=/absolute/private/paid-provider-authority.json
FIRECRAWL_RUNTIME_LEDGER_PATH=/absolute/private/firecrawl-runtime-ledger.db
FIRECRAWL_RUNTIME_MONTHLY_CALL_LIMIT=<reviewed positive UTC-month cap>
FIRECRAWL_RUNTIME_REQUESTS_PER_MINUTE=<reviewed positive rate>
ZOOMINFO_CREDIT_LEDGER_PATH=/absolute/private/zoominfo-credit-ledger.db
ZOOMINFO_MONTHLY_CREDITS=<reviewed positive Pacific-month cap>
```

Install the rotated credentials only on this host. Then run the read-only deep preflight:

```bash
python -m grant_watch.paid_provider_runtime_check
```

It must report `verified`. It validates the capability, permissions, provider/account
bindings, credentials, limits, reconciliation state, and ledger schemas without vendor
HTTP. Start exactly one listener, prove its revision/import path, and rerun the preflight.
Verify no old process or keepalive can relaunch old code. Provider calls remain disabled
until these checks and the vendor-side balance reconciliation agree.

## 6. Rollback and recovery

- Before any new-provider call, a failed application deploy may be rolled back using
  the normal database/code backup protocol, but leave paid credentials removed or set
  no paid credentials with `GRANT_PAID_PROVIDER_MODE=disabled`. Old code must never run
  with the rotated credentials because it can bypass the standalone ledgers.
- Never delete, replace, reset, or restore an older standalone ledger as rollback. It
  is spend history. Preserve it and its authority file even when the listener is down.
- If any provider request may have crossed HTTP after cutover, stop paid writers and
  reconcile the vendor account before restart. Do not infer a refund from a timeout or
  retry a possible charge silently.
- If migration or preflight fails, publish nothing else, keep all writers stopped, and
  investigate the refused invariant. Do not weaken file permissions, metadata binding,
  source enumeration, ceilings, or reconciliation circuits to make startup pass.
