# Validation records

## Two-bot administration candidate — 2026-09-17

This candidate adds a root-owned, exact-two-child administration boundary for
MacBot (`firstmate`) over Fin (`team-sandbox` / `secondmate`) and Toan
(`toanmytran-bot`). MacBot receives a scoped parent client and token, never the
Docker socket or operator token. Toan remains dormant. Tests used synthetic
tokens and identities; the Telegram credential disclosed in chat was not written
to the worktree and must be rotated outside chat before use.

### Automated checks

| Suite | Result | Environment |
| --- | ---: | --- |
| Parent control, administration, listener, storage and binding | 108 run: 106 passed, 2 skipped | Windows Python 3.12 |
| Parent control, administration, listener, storage and binding | 108 run: 107 passed, 1 skipped | WSL/Linux Python 3.10 |
| Child runtime and delivery | 75 passed in each environment | Windows and WSL/Linux |
| Data compiler/service/client | 30 run in each: 26 passed, 4 disposable-PostgreSQL skips | Windows and WSL/Linux |
| Constrained Git broker | 17 run: 15 passed, 2 root-only skips | WSL/Linux |
| Parent/child component contracts | 5 passed | WSL/Linux, real loopback HTTP |

Across both supported test environments, 448 tests ran: 435 passed and 13 were
explicitly skipped, with no failures. Static validation compiled 37 Python
files, parsed 12 JSON files, checked three
relevant shell scripts with Linux `bash -n`, rendered all three Compose projects
(MacBot, Fin and Toan), and passed `git diff --check`. The Windows integration
fixture is not authoritative because the production schema deliberately requires
absolute POSIX child homes; the same five contracts passed under WSL/Linux.

The administration tests cover exact operator/parent scope, denial for a third
registered child, pinned image/user/environment/labels/networks/mounts/restart
policy, private bind propagation, `AutoRemove=false`, no socket/host namespaces,
dormant-network lookup, bounded/redacted logs, fixed non-shell runbooks, durable
idempotent resource operations, serialized capacity-gated backups, exact nested
home binds, helper attestation, crash markers, restart verification and
marker-owned lease recovery. Listener tests cover global/per-IP admission, TLS
handshake placement, absolute header/body deadlines, slot cleanup, IPv4-only
operation and RFC1918-only plain HTTP. Storage tests cover SQLite/WAL/journal
ceilings and soft/emergency/hard write gates.

### Live rollout gates

This validation does **not** claim live activation. The read-only server preflight
found the broker registration scoped to exactly Fin and Toan and no Docker socket
in MacBot or either child, but these gates remain:

- Fin still has unresolved/uncertain work and an unverified runtime readiness
  state. Do not stop or recreate it until the queue is reconciled and an approved
  idle window exists. Its reviewed recreation must add bounded logs and the exact
  nested child-home bind before administrative attestation is enabled.
- Toan must remain `restart: "no"` and `start_allowed: false`. Revoke the token
  disclosed in chat, provision a fresh token directly into a UID-1000 mode-0400
  secret file, verify it resolves to `ToanMyTran_bot`, and review its independent
  trainer charter/captain identity. Activation then uses one broker-stopped,
  reviewed transition to `unless-stopped` plus a matching manifest/start gate.
- The root filesystem had about 7.2 GB free. Backup requires the larger of
  10 GiB or 10% of the filesystem (about 19.7 GB here), plus archive headroom,
  so backup is intentionally unavailable. No Docker prune or deletion is
  authorized by this candidate.
- Measure host RAM/CPU before setting MacBot's required resource-cap variables.
  Firewall broker port 8787 to only the exact MacBot, Fin and Toan bridge
  networks and prove an unapproved test network is denied.
- Merge this reviewed PR into `maycha-custom` before installing its artifacts.
  Publishing or validating the branch does not authorize deployment or merge.

## Baseline validation — 2026-09-15

The candidate was validated in disposable Linux containers on the deployment
server. Test fixtures contained synthetic identities, tokens and records. No
test wrote to the production Supabase database or sent a Telegram message.

## Automated checks

| Suite | Cases | Environment |
| --- | ---: | --- |
| Runtime and delivery upgrade | 57 | Python 3.12, Linux |
| Existing constrained Git broker | 14 | Python 3.12, Linux, local Git fixtures |
| Parent control, storage and binding | 17 | Linux, real loopback HTTP and POSIX locks |
| Data compiler/service/client | 26 | Linux, bounded fake database/HTTP fixtures |
| PostgreSQL integration | 4 | Disposable PostgreSQL 17 database |
| Parent/child component contracts | 3 | Real loopback HTTP and actual child SQLite queue |

The stock binding test loaded the real framework helpers from pinned revision
`b1ad702fafdd03d94e5ce47cd4ba86e589ad33d6`. It verified the child-local root and
parent report destination under the supported remote-filesystem marker without
creating fake parent launch metadata.

The PostgreSQL fixture used image
`postgres@sha256:051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0`
and the pinned driver requirements. It tested actual rows, RLS, aggregates and
keyset pages; seven denied DML/DDL operations in a READ WRITE transaction;
a synthetic PUBLIC SECURITY DEFINER writer and the READ ONLY/preflight defenses;
and rejection after schema drift. Its database and role were disposable.

The component contract tests covered parent HTTP request -> real child queue,
durable report intake, immutable knowledge proposal -> operator approval ->
selected-claim application, and parent policy export -> actual child sync.
Parent and child attempts to create approval were rejected. Notification checks
used an injected notifier and did not contact Telegram.

Additional checks: shell syntax, Compose configuration, the host service's
`--check` command under root-owned private paths, source secret-pattern scan,
Git diff whitespace checks and the published source manifest.

## Production facts and rollout gates

Only production database metadata and effective privileges were inspected.
The existing reader has no discovered table/column/sequence write, schema CREATE,
admin/bypass, inherited-role or callable non-system SECURITY DEFINER privileges.
The new dedicated-reader provisioning file remains an operator-reviewed template;
no production role, policy or record was changed by this validation.

The current firstmate and secondmate were not replaced by this PR. Before live
rollout, reconcile active/uncertain work, bind the child while stopped, configure
the private parent API and restricted data manifest, and then verify real Codex
acceptance, group/topic replies, actual Mac proposal notification, stock scout
cleanup and guarded lifecycle operations. Offline tests do not prove these live
effects. Follow the component guides and record the deployed commit afterward.
