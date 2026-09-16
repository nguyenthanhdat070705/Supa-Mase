# Validation record — 2026-09-15

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
