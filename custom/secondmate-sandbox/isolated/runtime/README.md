# Isolated persistent child runtime

This runtime keeps the existing Docker/tmux secondmate and adds a real parent
adapter to primary firstmate. It accepts scoped parent requests and authorized
team Telegram messages, reports outcomes upward, and keeps each child's memory
and work private. The initial instance uses `gpt-5.6-sol` / `xhigh`.

## Configuration and separation

Use a separate deployment directory, container, HOME, bot, control token and
instance manifest for every child. Copy `../instance.example.json` to the host's
`instance.json`; its read-only mount fixes child ID, parent, own home, tmux target,
bot username, launch model and excluded group IDs. Never share HOME/queue/session
volumes between children. Use `../secondmate.env.example` for verified Telegram
identity and one-time group bootstrap values. Actual secrets remain private.
For each additional instance, change the compose service/container name and its
own HOME/bot/token mounts, then register that exact container and child ID in the
host parent registry; changing the manifest alone does not create another child.

The compose mounts are deliberately narrow:

- Independent `home/` at `/home/nguye`; a separately installed Codex auth copy is
  an operator prerequisite, not an inherited memory/configuration mount.
- This source directory and `instance.json` read-only at `/opt/secondmate`.
- This child's bot token and parent-control token as separate read-only files.
- Only `data-access/data_client.py` from the database package. No database DSN,
  server module, primary GitHub credential, firstmate HOME or Docker socket.

The derived image preserves Codex under `/opt/codex` so mounting HOME cannot
hide it. `firstmate:local` and a reviewed Linux `treehouse` binary are build
prerequisites; see the top-level reproduction guide for exact versions. Existing
Git transport remains the constrained host broker. This change does not replace
that broker, create production access, merge PRs or deploy source changes.

## Parent binding and startup

The official helper seeds the `demand-planning-maycha` direct-PR child from the
pinned framework clone at `/home/nguye/provisioner`. The clone is a source of
framework code, not a live parent. Provisioning/charter assembly remain explicit
operator steps. Do not start compose in an unseeded child working directory.

For the existing deployment, stop the service only after reconciling active
work and backing up its independent HOME/SQLite database consistently. Run the
host-side `parent-control/binding.py` as the child-home owner with the actual
`--home`, `--child-id`, `--parent-id` and `--parent-url`. The helper requires the
service lock to be free, preserves the old marker, and writes:

```
.fm-secondmate-parent                 # schema=fm-secondmate-parent.v1, route=remote
data/parent-control-binding.json       # actual parent/child/service identity
```

`route=remote` expresses the separate filesystems; this is the custom container
adapter, not the framework's native remote-spawn backend. Do not fabricate local
`fm-spawn` metadata. Register the existing container in the host parent service,
install its child-scoped token, and make the configured control URL reachable
from Docker's host gateway. Copy the updated domain charter and
`secondmate-transport.md` into the seeded child's `data/` before launch.

On startup, the entrypoint checks both markers and the binding against the
instance. The main agent reads its charter and calls:

```
python3 /opt/secondmate/bridge.py ready --proof SECONDMATE_READY
```

This must be a real agent tool call. The bridge proves launched PID/start/nonce,
own home, exact immutable pane, verified Codex executable (including its anchored
npm Node wrapper), foreground process group and tool ancestry. It binds the
exact Codex thread/session IDs and matching session metadata. Readiness waits
for that same model turn's `task_complete`; it does not enable intake while the
startup turn is still working. Verify model/effort independently in runtime UI
or session metadata. Do not manufacture readiness JSON.

## Delivery and dynamic groups

The durable SQLite queue records Telegram updates before advancing the cursor.
Duplicate updates do not replace earlier state. Parent UUID requests use stable
negative internal IDs, leaving Telegram cursor semantics unchanged. Parent
request limits match the host: nonblank body up to 100000 UTF-8 bytes, no NUL,
and a JSON object scope up to 8000 canonical UTF-8 bytes.

Only the fixed shell-comment pointer `# SECONDMATE_INBOX ID /fixed/path/ID.json`
enters tmux; Telegram text is data inside the inbox. Paste and Enter use the
verified immutable pane ID, with a short paste-settle delay and an identity
recheck before Enter. `delivered` now requires a new exact user-input record in
the bound Codex session after the saved attempt cursor. A successful tmux command
alone is insufficient. Missing proof becomes `unacknowledged` and reports an
operator-attention event upward. No automatic replay or second Enter occurs.

Each accepted request needs `notify --update ID --file PATH`, then
`complete --update ID`. Telegram replies retain original chat/topic/message;
parent requests send a correlated host event. Telegram outcomes also enter the
parent report outbox. Completion consumes a real agent proof and waits for the
matching turn to finish before allowing another request. Compare-and-set state
transitions prevent a delivery acknowledgment from overwriting a concurrent
completion. Stock `state/parent-replies.status` records are relayed durably with
stable event IDs. The old provisioner status file retains its separate cursor
and is drained during transition; never delete either report stream.

Real member text in enabled groups becomes team work. Captain identity comes
only from Telegram's verified numeric `from.id`, including in groups. Bots,
anonymous/channel senders and unknown users in private chats are ignored.
Only captain can use `/group_on`, `/group_off` and `/groups`, optionally addressed
to this bot. Use a numeric target only in captain DM; in a group the command
applies to that group. Enrollment verifies the bot's membership. Operator
excluded groups cannot be enrolled. Registry changes are audited and survive
restart; env group IDs seed the registry once. Revocation is rechecked before
queued delivery and outbound replies. Earlier ignored messages are not replayed.

## Supervision, inheritance and learning

The parent API supplies scoped requests, outcome collection, policy snapshots
and guarded lifecycle operations. It exposes no child approval capability.
`control_client.py sync-brain` installs only the approved configuration paths and
curated skills after revision/hash verification, refusing to overwrite local
edits. Model changes take effect on an authorized fresh launch. It imports no
primary private memory, backlog, authentication, mutable tool database or
session history. Snapshot application may be resumed after a partial write;
files removed from a snapshot are not automatically deleted.

`knowledge.py propose --file PATH` stores immutable child-local claims/evidence,
version and content hash, submits them to parent and sends Mac a private notice.
A host receipt is not approval. Only explicit approval of the exact proposal
version/hash and selected claims permits parent-side import. The child has no
approve/apply command. Notification uncertainty is journaled and never silently
retried. Local learning continues inside the child regardless of proposal status.

The mounted `data_client.py` uses child authentication for structured schema and
read queries. The host derives child scope and holds the database credentials.
See `data-access/README.md` for supported operations and their limits.

## Recovery and guarded lifecycle

`bridge.py status` returns counts, active request metadata, dynamic groups,
minimal captain-authenticated onboarding candidates and verified readiness.
`reconcile-delivery` checks acceptance evidence only; it never pastes or submits.
A crash/timeout can leave `delivering`, `uncertain` or `unacknowledged`; keep the
request and inspect the existing composer plus exact bound session before an
explicit operator action. Do not reset cursors, delete the queue or blindly
rewrite statuses. Legacy in-flight rows without attempts require manual review;
they are retained by schema migration. Parent outages do not block durable
Telegram intake, and parent event retries reuse the same idempotency key.

`control-check --quiesce --expected-generation NONCE` acquires a durable lease
only for the verified idle generation with no active/uncertain request or worker.
The parent rechecks the same lease/container before stopping or restarting it.
`control-resume --lease-id UUID` releases it; a fresh launch clears an old lease.
An interrupted/aborted turn without a matching completion fails closed and
requires reconciliation. Normal lifecycle commands never force-stop such work.

Readiness is not a generic terminal modal detector. Keep the agent pane dedicated;
stop/quiesce the bridge before interactive operator maintenance. Process checks,
paste and log acknowledgment are not one atomic operation. Telegram sends also
lack transactionally coupled idempotency, so uncertain sends require inspection.
The queue/registry enforce transport policy for cooperating child processes;
they are not a hostile-code boundary against the same UID editing its own HOME.
Host token scopes, constrained Git and database read restrictions are separate
boundaries. Keep private HOME/state out of commits.

## Offline checks and delivery

From this directory:

```
python3 -m unittest -v test_bridge.py test_upgrade.py
```

The tests use mocked Telegram/control endpoints and synthetic Codex logs. They
cover identity/foreground races, exact session acknowledgment, aborted turns,
no-replay recovery, concurrent completion, parent size/idempotency contracts,
dynamic captain administration, group revocation, lifecycle leases, curated
brain paths and immutable knowledge notifications. A real startup smoke check
is still required when the approved change is deployed to Linux.

Commit and push all source changes on a dedicated branch, open a PR against
`maycha-custom`, and ask Mac to review and merge. This publication does not
restart the running child or authorize auto-merge/deployment.
