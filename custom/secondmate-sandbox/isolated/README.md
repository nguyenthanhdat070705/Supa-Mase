# Firstmate and its team secondmates

This package gives the existing Linux firstmate a parent control adapter for
separate secondmate containers. The current child uses its own Telegram bot,
Codex `gpt-5.6-sol` with `xhigh` reasoning, and private working memory. Additional
children receive independent identities, homes, queues and credentials.

## Team workflow

1. Captain adds the child bot to an intended group and enables that group using
   the captain-only onboarding command. Members can send work to that bot.
2. The child uses the parent-approved runtime configuration and curated skills.
   Firstmate can also send a correlated task and collect its outcome through
   the parent client.
3. Instructions and lessons from team members stay in the child's own memory.
   Useful lessons become immutable proposals with claims, evidence and a content
   hash. Captain receives the proposal for review.
4. Captain chooses the exact proposal and claims to promote. The parent records
   approval and applies those claims into its own reviewed knowledge. A group
   message or child credential cannot approve or apply that promotion.

Here, learning means instructions and stored knowledge; this package does not
fine-tune model weights. Sharing approved configuration and skills does not copy
the parent's conversations, credentials or entire private memory.

## Components

| Path | Purpose |
| --- | --- |
| `parent-control/` | Trusted host API, parent client, immutable reports/proposals, approved knowledge import and child lifecycle controls |
| `data-access/` | Structured PostgreSQL reads with a restricted role and per-child table/column policy |
| `runtime/` | Child Telegram intake, durable work queue, Codex session acknowledgment and parent/data clients |
| `compose.yml` and `instance.example.json` | Separate child container, home, model, tmux identity and resource limits |
| `charter-domain.md` | Delegation, data access, local learning and captain approval rules |
| `git-broker/` and `install-broker.sh` | Host-held GitHub credentials; constrained child pushes and PR creation |
| `examples/` and `secondmate.env.example` | Sanitized configuration templates; no live credentials |
| `SOURCE-MANIFEST.json` | Published file hashes and Git executable modes |

Read the [reproduction guide](REPRODUCE.md),
[parent adapter guide](parent-control/README.md),
[data access guide](data-access/README.md),
[runtime guide](runtime/README.md) and [Git broker guide](git-broker/README.md).

## Parent and child boundaries

The parent adapter is an explicit container transport. The stock framework's
remote marker supplies cross-filesystem lifecycle compatibility; this package
does not pretend that a tmux session is an official Herdr remote session. The
trusted host registry identifies the actual firstmate and its children.

The host owns the registry and parent/child credentials. The parent uses a
scoped client without receiving a Docker socket. The child receives neither
parent credentials nor a mount of parent memory. Parent tasks, child reports,
knowledge proposals and approvals have durable identifiers and strict scopes.

Captain identity comes from Telegram's numeric sender ID. Group membership is
enabled explicitly; the configured excluded company group remains forbidden.
Replies retain their originating chat, topic and message. The runtime registry
and Telegram transport share the child UID: these are application controls,
not protection against arbitrary code already running as that same UID.

## Supabase reads

Children submit structured table, filter, ordering and aggregate requests to
the host API. They receive no database password or service-role key, and no raw
SQL or RPC endpoint. The host connects directly to the selected Supabase
PostgreSQL database using an audited restricted role.

Every request checks the approved schema metadata, opens a read-only
transaction and enforces time, row and response-size limits. Tables containing
credentials, unrestricted configuration, bot state, system data, unreviewed
views or executable dependencies are not enrolled by default. New tables and
schema changes require a reviewed policy update. Large tables support bounded
keyset pagination and server-side aggregates.

The supplied role/policy SQL and production metadata review are separate from
installation. Deployment must refuse database access until the actual role,
RLS policies, credentials and table manifest pass validation. Do not put the
live manifest, credentials or exported business rows in this public repository.

## Delivery and recovery

The runtime binds readiness to the exact Codex thread and its startup turn.
Delivery is acknowledged only when that thread records the actual inbox pointer
as a new user message. It waits for startup completion, delays Enter after paste
and retains uncertain deliveries for reconciliation. It never automatically
replays an uncertain request. Telegram delivery also has an uncertainty window;
successful local state alone is not proof of an upstream message or Git push.

Run all component unit suites and the disposable PostgreSQL integration fixture
described in their guides before rollout. A live rollout also needs a bot-group
request, parent-to-child task, captain proposal notification and reviewed
knowledge promotion check. Offline tests cannot prove those external effects.

## Publishing and rollout

All source changes go to a dedicated GitHub branch and PR against
`maycha-custom`. Ask Mac to review and merge. Do not auto-merge. Publishing a PR
does not deploy it. Reconcile active/uncertain work before upgrading a live child
and record the deployed source revision after the approved rollout.

All live services and validation containers run on the Linux server. The desktop
is only the control and source preparation environment.
