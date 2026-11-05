# Supabase-hosted dashboard

`natsuhat view --hosted` explicitly enables Supabase Auth, PostgreSQL, and private
Storage. Setting credentials alone does **not** change the local CLI or upload
local history. Never enable hosted mode against a project without permission to
store its scans and source code there. No migrations run at application startup.

## Configuration

Keep these variables in your deployment secret manager (Amp Secrets for orbs),
not in Git or a frontend build. Even the publishable key is used server-side.

| Variable | Value |
| --- | --- |
| `SUPABASE_URL` | Project API URL, e.g. `https://PROJECT.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | New publishable API key |
| `SUPABASE_SECRET_KEY` | New backend secret API key; never a browser credential |
| `DATABASE_URL` | Restricted runtime role, direct or **session-pooler** PostgreSQL URL |
| `SUPABASE_S3_ENDPOINT` | Storage S3 endpoint from the project's Storage settings |
| `SUPABASE_S3_REGION` | Region shown with that endpoint |
| `SUPABASE_S3_ACCESS_KEY_ID` | Supabase S3 access key, not the publishable API key |
| `SUPABASE_S3_SECRET_ACCESS_KEY` | Matching S3 secret |
| `NATSUHAT_SESSION_ENCRYPTION_KEY` | Stable, randomly generated secret of at least 32 characters |
| `NATSUHAT_PUBLIC_URL` | Exact dashboard HTTPS origin, with no path or trailing slash |

URL-encode special characters in PostgreSQL passwords. TLS defaults to
`sslmode=require`; prefer `sslmode=verify-full` with the Supabase CA installed.
Do not use transaction pooling: the controller holds a **session advisory lock**.
Keep PostgreSQL and S3 connections private to the backend. The runtime login must
not own the tables, have `BYPASSRLS`, or be a superuser.

Generate the session secret with `openssl rand -base64 48` and store its output
privately. Rotating it invalidates existing dashboard sessions. Back it up
separately from the database. The app stores only opaque session-cookie hashes
and encrypted Auth access/refresh tokens; no passwords or browser local-storage
tokens are persisted.

## Explicit project setup

Use a **migration/admin connection**, not the runtime login, to apply
`supabase/migrations/202611050001_hosted_dashboard.sql` once. It creates the
`natsuhat` schema, RLS policies, two no-login application roles, and private
`scan-artifacts` and `scan-sources` buckets. Do not expose `natsuhat` through
Supabase's Data API or grant it to `anon`/`authenticated`.

Create a separate login using your database admin tooling and a generated
password, then grant it only the application roles:

```sql
-- Substitute your runtime login name; create its password through secure tooling.
alter role natsuhat_runtime nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant usage on schema natsuhat to natsuhat_runtime;
grant natsuhat_api, natsuhat_worker to natsuhat_runtime;
```

Configure `DATABASE_URL` with this login. Authenticated reads use `SET LOCAL ROLE
natsuhat_api`, verified user identity, and membership RLS in every transaction.
Backend publication and authorized mutations use `natsuhat_worker`. The backend
is trusted: this login and the S3 credentials can access all workspaces. Never
pass them to scan workers or expose a SQL endpoint.

In Supabase Auth:

1. Enable email/password and TOTP MFA. Use an asymmetric JWT signing key (ES256
   or RS256); legacy HS256 JWTs are intentionally rejected.
2. Disable public sign-ups and anonymous sign-ins. Configure your dashboard
   origin as Site URL. Keep redirect allowlists restricted to that origin.
3. Configure production SMTP and email sender/domain; the built-in mail sender
   has restrictive delivery limits. Configure abuse/rate limits at your HTTPS
   proxy too. The app does not trust arbitrary `X-Forwarded-For` headers.
4. Replace the invitation link template with
   `<a href="{{ .SiteURL }}/#type=invite&token_hash={{ .TokenHash }}">Accept invitation</a>`.
   Replace password recovery with
   `<a href="{{ .SiteURL }}/#type=recovery&token_hash={{ .TokenHash }}">Reset password</a>`.
   These fragments keep tokens out of HTTP logs and require a user click before
   consumption, avoiding email scanner prefetch. Default implicit-token links
   are not supported. Test both templates before inviting real users.
5. Create and verify the first owner account through Supabase's admin tooling,
   then bootstrap one workspace using that Auth user UUID:

```sql
begin;
with workspace as (
  insert into natsuhat.workspaces(name) values ('My workspace') returning id
)
insert into natsuhat.workspace_members(workspace_id,user_id,role)
select id, 'REPLACE-WITH-VERIFIED-AUTH-USER-UUID'::uuid, 'owner' from workspace;
commit;
```

The owner signs in and enrolls/verifies MFA before seeing workspace data. Owners
can create workspaces and transfer ownership. Owners/admins can invite members,
change roles, remove members, configure providers, and delete terminal scans.
Operators can execute scans **after MFA** and view results. Viewers can only
browse results. Existing verified accounts accept invitations on their next
sign-in; new accounts receive Supabase email. Recovery still requires existing
MFA before a password change. Factor removal revokes all dashboard sessions;
password changes revoke the user's other dashboard sessions.

## Run the backend

Use HTTPS in production and keep the upstream port behind the reverse proxy.
POST requests require the configured exact public Origin. There is no shared
viewer token in hosted mode and no client-side service key or direct bucket URL.

```bash
uv run --frozen natsuhat view --hosted --no-open --host 0.0.0.0 --port 8080
```

For the existing VPS stack, explicitly select both Compose files:

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.supabase.yaml up -d --build
```

In Komodo, add `deploy/compose.supabase.yaml` to the stack's Compose file list
and store variables in its secret configuration. This does not enable or trigger
deployment automation. Never print resolved Compose configuration containing
real credentials.

## Durability, limits, and recovery

- Workspace requests carry the workspace displayed in that browser tab. If another
  tab changes the shared session's workspace, stale requests receive HTTP 409
  until the tab refreshes its session; they cannot silently target the new workspace.
- Hosted JSON bodies have a 30-second total deadline; source ZIP uploads have a
  five-minute total deadline, including clients that trickle bytes. Uploads do not
  hold the dashboard-wide mutation lock, so other users can still sign out or
  stop scans. The supplied Caddy configuration also limits header/body reads.
- A scan and its immutable source must persist before the paid worker launches.
  Workers receive no Supabase/database/session secrets. Results use the existing
  allowlist, including SQLite backups of agent checkpoints, rather than whole
  sandbox exports. Job logs and provider/GitHub credentials remain local.
- Live snapshots enter a disk-backed retry journal and publish on a five-second
  controller loop. Objects upload before one transaction updates the current
  generation, findings, and transcript. Older retries cannot replace newer
  results. Withdrawal removes findings from the current catalog. The latest
  complete generation is retained, not every historical generation.
- Unused ZIP uploads expire after 24 hours. Scan sources remain until scan
  deletion. Deletion hides data transactionally before local cleanup; object
  deletion retries durably and does not download archived results first. A
  Storage outage or corrupt archive does not prevent deleting a safely stopped
  scan. Superseded objects are garbage-collected; abandoned
  uploads/publications have a 24-hour grace period. Cleanup runs while the backend
  is up, not as a Supabase scheduled job. Retained E2B VMs are **not** deleted.
- Preserve `/var/lib/natsuhat`, including its journal, sources, workspace settings,
  and provider credentials. A DB or Storage outage can delay final publication;
  local pending results must not be discarded. Stop scans and wait for pending
  publication before upgrades. This remains a **single controller**, not an HA
  supervisor; losing the lock stops its workers, and upgrades can interrupt scans.
- Completed records are readable from PostgreSQL without local files. For an
  operator action the backend restores checkpoints/sources from private Storage.
  Resume still requires the retained E2B VM; restoring files does not recreate a
  VM or its running processes. If local disk is lost during an active scan, the
  app refuses to adopt an ambiguous worker automatically. An operator must check
  E2B and reconcile the scan before continuing.
- Set project/bucket limits to accommodate existing 100 MiB source ZIPs and large
  result archives. Supabase Free's 50 MB maximum is insufficient for the full ZIP
  contract; use a suitable paid plan. Storage, database, egress, SMTP, E2B, and LLM
  costs are separate. Database backups do **not** back up Storage file bytes;
  arrange and test private object backups separately, including deletion policy.
- Existing local runs are not imported automatically. Keep using the local viewer
  for them; bulk import, workspace deletion, and editable finding triage are not
  included in this first integration. The schema preserves triage on republish.

## Local contract tests

Normal tests require no Supabase account. The hosted integration suite additionally
needs a disposable TLS-enabled loopback PostgreSQL database whose name begins
`natsuhat_test`. It **drops its test schemas**. Set only
`NATSUHAT_TEST_DATABASE_URL`, never a real project URL, then run:

```bash
uv run --frozen pytest -q tests/test_viewer_supabase.py
```

Tests implement synthetic Auth/JWKS and S3 HTTP endpoints, use real PostgreSQL RLS
under a non-owner runtime role, and never run a pentest or contact Supabase.
The dedicated CI job provisions disposable PostgreSQL and runs this suite.
