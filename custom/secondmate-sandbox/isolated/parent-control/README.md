# Linux control service and scoped MacBot client

The deployed parent is container `firstmate` on the same Linux host. It connects
outbound to this root-owned broker using the scoped client and parent token,
without a Docker socket. For the optional user-installed Windows alternative,
see [Offline escalation protocol and portable receiver](OFFLINE-ESCALATIONS.md).

Production `firstmate` owns each configured child's requests, report inbox,
policy publication and controlled lifecycle. The trusted host service resolves
fixed container/home identities; parent and child clients use distinct scoped
HTTP tokens and never receive a Docker socket or Docker privileges.

This is an explicit container adapter, **not** a stock Herdr remote backend.
Do not fabricate native `state/*.meta` entries or insert a fake native remote
route into `data/secondmates.md`. Keep a plain-prose pointer there to this
adapter's authoritative host registry and parent control skill/CLI. Root's
`server.json` is the machine-authoritative parent -> child map. `list` returns
only the authenticated parent's children.

## Files and trust

| File | Responsibility |
| --- | --- |
| `server.py` | Authenticated API, fixed runtime dispatch, durable request/event/proposal/approval/operation SQLite records |
| `lifecycle.py` | Existing-container identity/generation checks, runtime quiesce lease, start/stop/restart/resume |
| `administration.py` | Exact-child attestation, diagnostics, redacted logs, fixed runbooks, resource profiles and capacity-gated state backup |
| `client.py` | Parent CLI and durable report collector; no Docker dependency |
| `local_ops.py` | Allowlisted brain export, separate report intake, approved selected-claim promotion |
| `binding.py` | Offline, locked migration to supported cross-filesystem marker plus explicit actual-parent manifest |
| `common.py` | Versioned wire schemas, path bounds and content digests |

The host service is trusted and can use Docker. Install its code and config
root-owned and not writable by either agent. Secrets/config are mode 0600; the
state directory is root-owned mode 0700. Parent token can control only its own
children. Child token can report/propose/fetch policy and query its separately
configured read-only data scope; it cannot list parents, issue parent requests,
control lifecycle, approve proposals, or apply knowledge.

An optional, separate top-level `administration.children` map is the root-owned
administrative allowlist. Registration as a normal child does not grant these
rights. Each administrative entry pins its container name, immutable image ID,
user, complete mount destinations/read-write flags, deployment labels, exact
restart policy, start gate, resource profiles, backup paths and runbook names. The live deployment
should contain only the exact children MacBot is meant to operate. A future
third child remains outside this surface until a root operator deliberately adds
a complete manifest.

**Only the separate operator token can create a knowledge approval.** Never
install that token in firstmate or secondmate. The trusted operator must inspect
Mac's actual explicit approval of the exact proposal hash, selected claims and
destination before recording it. `approval_reference` records that evidence;
it is not an independent Telegram signature or proof. Neither a model's report
nor a made-up reference is authorization. There is no automatic approve/apply.

Tokens are loaded from private files, never returned by the API, passed in
process arguments, or logged. TLS uses standard certificate verification and
clients refuse redirects. For a reachable Docker bridge listener, set the
specific host bridge address and TLS certificate/key. An explicitly configured
`allow_private_http: true` is an operator choice for a firewall-restricted,
trusted private bridge; the default loopback listener cannot be reached from
agent containers. Do not expose this service publicly. The example IDs are
synthetic and must be replaced with independently verified numeric identities.

The host-root listener admits at most `max_http_workers` (4–32, default 16)
concurrent requests and `max_http_workers_per_ip` (1–8, default 4) from one
container/source address. It applies `header_timeout_seconds` (2–20, default 10)
before request-line, header, authentication or body handling, and closes excess
sockets without allocating a thread. The framing limit is an absolute deadline,
not an idle timeout; POST bodies have a separate absolute 40-second deadline.
TLS handshakes run inside the same bounded workers. The bundled listener is
IPv4-only; use `127.0.0.1` or an explicit private bridge address.
The service unit also caps tasks, resident memory and file descriptors; these
controls complement the bridge firewall and per-token scope rather than replacing
them.

Every POST also passes a durable-storage gate. SQLite has a root-configured page
ceiling (512 MiB by default), bounded WAL checkpoint/journal settings, a 2-GiB
filesystem reserve, and worst-case admitted-request headroom before another write
is accepted. Audit rows are never silently deleted. At the cap, new writes return
`storage_blocked` until reviewed offline archival or capacity work; monitor the
broker filesystem and keep it separate or quota-controlled in production.
Bulk intake stops with 128 MiB still reserved. Authenticated lifecycle control
and fixed resource-profile containment may use a smaller 16-MiB emergency tier;
even those operations fail closed before the hard byte/reserve boundary.

## Protocol

All API calls use `Authorization: Bearer <scoped-token>`. POST uses JSON,
Content-Length, and at most 2 MB. Unknown fields/identities are refused where
they would widen authority. Child identity comes from its credential, never
from Telegram text. Request text, reports, evidence and proposals are data,
not trusted instructions to the parent.

| Endpoint | Authority | Meaning |
| --- | --- | --- |
| `GET /v1/children` | Parent | Its registered children |
| `GET /v1/children/ID/status` | Owning parent | Bridge status plus container/runtime generation |
| `POST /v1/children/ID/requests` | Owning parent | Durable correlated request before runtime enqueue |
| `POST /v1/children/ID/requests/retry` | Owning parent | Explicit same-ID/body retry after unknown delivery |
| `POST /v1/children/ID/control` | Parent or scoped operator | Guarded existing-container lifecycle |
| `POST /v1/children/ID/reports` | That child | Immutable event UUID, optional matching request/correlation |
| `GET /v1/reports?after=N` | Parent | Ordered durable report events for its children |
| `POST /v1/children/ID/proposals` | That child | Immutable proposal ID/version/content hash; queues parent review event |
| `GET /v1/children/ID/proposals` | Owning parent | Proposal version/hash index |
| `GET /v1/children/ID/proposals?proposal_id=UUID&version=N` | Owning parent | One exact proposal for review |
| `POST /v1/children/ID/approvals` | Separate scoped operator | Record externally verified Mac approval |
| `GET /v1/approvals/UUID` | Owning parent | Exact approved claims/version/destination |
| `POST /v1/approvals/UUID/receipt` | Owning parent | Idempotent evidence of local application |
| `POST /v1/children/ID/brain` | Owning parent | Publish allowlisted versioned snapshot |
| `GET /v1/children/ID/brain` | That child or owner | Current immutable snapshot |
| `POST /v1/data/query`, `/v1/data/schema` | Child or owning parent | Delegates to separately configured `data-access` service |
| `GET /v1/children/ID/admin/diagnostics` | Owning parent or scoped operator | Attested isolation, lifecycle and resource state |
| `GET /v1/children/ID/admin/logs?tail=N&since_seconds=N` | Owning parent or scoped operator | Bounded, sanitized, non-following diagnostics |
| `GET /v1/children/ID/admin/runbooks/NAME` | Owning parent or scoped operator | One fixed read-only maintenance runbook |
| `POST /v1/children/ID/admin/operations` | Owning parent or scoped operator | Idempotent resource-profile change or state backup |
| `GET /v1/children/ID/admin/operations/UUID` | Owning parent or scoped operator | Durable operation result/unknown state |
| `GET /v1/children/ID/admin/backups` | Owning parent or scoped operator | Root-held metadata for this child's backups |

Data child credentials supply their own scope and cannot submit `child_id`.
Parents may name only their configured children, receiving the same limited
data view. Set `data_access_config_file` to the private operator data config.
The adjacent `data-access/data_service.py` owns SQL/catalog/role enforcement.
When absent, these endpoints refuse as unconfigured.

### Requests and reports

Parent request body is `{request_id: UUID, correlation: 16-lowercase-hex,
body: string, scope: object}`. The host adds `schema: parent-request.v1`, the
fixed `child_id`, and `authority: {role: parent, approval: false}`. It executes
only the fixed command:

```text
docker exec -i --user FIXED_UID:GID --env FM_HOME=FIXED_HOME FIXED_CONTAINER
  /usr/bin/python3 /opt/secondmate/bridge.py enqueue-parent --request-file /dev/stdin
```

No shell evaluates the envelope. `delivery=delivered` proves durable runtime
enqueue, **not** agent acceptance, completion, or a reply. Only a matching
outcome event resolves the work semantically. An unknown delivery retains the
request; repeat the same request ID and bytes explicitly. No automatic new
agent is spawned on a timeout.

Report event has canonical UUID `event_id`, `kind` (`done`, `blocked`, `failed`,
`progress`, `decision`, `pr-ready`, `knowledge-proposal`), and bounded `text`.
Optional `request_id` and `correlation` must occur together and match a request
for this exact child. Repeating the same event is idempotent; changing it is
refused. Parent collector stores each event under
`data/secondmate-inbox/events/UUID.json` before advancing its durable cursor.
This review directory is deliberately separate from captain/learnings memory.

The child runtime separately notifies Mac of a submitted knowledge proposal.
Parent `pull`/`collect` also surfaces `notification_required`. An optional fixed
`notifier_command` argv in the parent client config can deliver these review
events to the already-authorized Mac DM transport, receiving one event JSON on
stdin. No destination or token is inferred by this adapter. Callback output is
never exposed. A timeout or nonzero callback result is recorded as unknown and
requires inspection; it is never automatically replayed. Callbacks should
deduplicate the supplied `event_id` too. No example command sends test messages.

### Lifecycle

Control body contains an immutable `operation_id` UUID, `action`, and the exact
`expected_generation` returned by status. Normal stop/restart requires a
verified idle runtime, no active/uncertain requests, no active crew records,
and a matching durable quiesce lease from `control-check`. This closes intake
delivery while the host rechecks runtime and container identity. Uncertain
checks refuse. `resume` releases the exact quiesce lease. A stopped container
uses its `container:SHA256` generation for `start`; an already running one is
never started again. Lifecycle control never creates or replaces a managed bot
container.

Only a separately configured operator with `allow_recovery: true` may supply
`operator_recovery_reference` to recover an active or unverified runtime.
That explicit recovery may interrupt work, but never removes volumes or work.
The operation journal preserves executing/unknown results instead of blindly
repeating maintenance. Ordinary lifecycle operations are not autonomously
replayed; inspect status and use `resume` if a stop/restart did not complete.
The narrower backup recovery path is described below.

### Scoped administration

The parent still has no Docker CLI/socket. The trusted host resolves the child
ID through two independent root-owned maps, inspects the configured name, checks
the pinned image/user/labels/security/mount/environment/network/log-rotation
manifest, and then operates only on the resulting immutable container ID. Every
mutation requires the current runtime generation, an idle/quiesced child, a
per-child lock and an operation UUID/content hash journal. A crash is recorded
as `unknown`; it is never blindly replayed. `start_allowed: false` blocks a
dormant instance such as a bot that has not yet received its independently
verified Telegram identity.

Logs never follow and are capped by line count, lookback and response bytes.
Known broker tokens and common API/Telegram/JWT credential forms are redacted;
control characters are removed. Runbooks are the fixed names `runtime-health`,
`home-usage` and `processes`; no endpoint accepts shell, argv, host path,
container, environment or signal input.

Resource changes select one root-configured profile. They do not accept raw
Docker flags. State backup first obtains the exact quiesce lease and storage
estimate, stops the attested bot, and starts one temporary helper from the same
pinned image. The helper has no network, capabilities, writable root or logs. It
receives read-only only the exact, pre-provisioned child-home bind already pinned
in the managed bot; its parent-home, Docker secret and runtime mounts are not
inherited. Administrative manifests refuse ancestor-only, shared or overlapping
home sources across managed children.
Its fixed `tar` invocation starts at the configured bot home, names only the
configured relative `config/`, `data/` or `state/` paths, and does not dereference
symlinks. It streams the archive to a root-only broker directory; `.codex`, secrets,
absolute/traversal paths and caller-selected paths are refused. Backup is denied
before creating a partial file unless the estimated state is within quota and
free space after staging remains above both the byte reserve and filesystem
percentage reserve. The helper is removed, the exact bot ID is started and the
lease is released before a response. After an exclusive broker restart, a bot is
started only when an exact `executing` backup journal row and matching durable
marker prove that this operation observed it running and completed the stop.
Ambiguous, stale or pre-stop markers fail closed and leave a stopped bot stopped
for offline operator review; a surviving exact lease may still be released when
the runtime is verified. Only root-recorded, fully attested helpers are removed.
Archives can contain trainer memory or any data the bot copied into an allowed
path, so no API exposes their contents and they must remain root-only. With the
default reserve, backup is intentionally unavailable unless free space remains
above the larger of 10 GiB or 10% of that filesystem, plus archive headroom. On
a roughly 197-GB filesystem, the percentage gate is about 19.7 GB.

There is deliberately no HTTP shell, arbitrary exec, secret rotation, mount or
network change, build/pull/prune, container/data delete, restore, or unreviewed
image rollout. Restore and destructive recovery remain offline operator actions
using the separate token/evidence path; this prevents a prompt-injected parent
from turning two-bot authority into host-root authority.

## Brain and knowledge schemas

Brain snapshot: `{schema: brain-snapshot.v1, revision, source_commit,
files: [{path, sha256, content}]}`. Files are sorted uniquely by path. Revision
is SHA256 of canonical JSON of the files array; file hashes cover UTF-8 bytes.
Canonical JSON sorts object keys, uses comma/colon separators, and does not
ASCII-escape Unicode. Export includes only these configured files and explicitly
selected `.agents/skills/NAME/` trees:

```text
config/inherited-runtime.json
config/crew-dispatch.json
config/crew-harness
config/backend                         (only with --include-backend)
config/backlog-backend
config/startup-memory-budget
```

Never exports `.env`, auth files, history, captain.md, learnings.md, whole data/
or arbitrary skill paths. Skills must be deliberately curated; text files are
bounded, links refused, and common literal key material causes refusal. This
scan is supplementary: the operator still reviews the selected files. Optional
`--model` and `--effort` generate only the explicit inherited-runtime config,
not credentials or model history. Skill assets are data supplied by a trusted
parent; runtime applicability is still constrained by the child charter.

Knowledge proposal schema is `knowledge-proposal.v1` with UUID `proposal_id`,
positive integer `version`, `manifest`, `content`, and `sha256`. Manifest has
`claims: [{id,text,evidence_ids:[id]}]`,
`evidence: [{id,description,source}]`, and `scope: {domain: string, ...}`.
The hash covers canonical JSON of the full proposal without `sha256`.
Versions cannot be overwritten. Receiving or reviewing one imports nothing.

An operator approval binds exactly that hash/version, selected claim IDs,
captain reference, parent identity and one of `data/learnings.md` or
`data/knowledge-reviewed.md`. Parent `apply` fetches the approval from the API,
appends only selected claims with evidence/provenance, then writes an idempotent
local and host receipt. It never copies the proposal's full notes or other
claims. Symlink/hardlink destinations refuse. A crash between target and receipt
writes is recovered by its exact content marker, without a duplicate append.

## Parent commands

Install these modules inside firstmate at an operator-chosen read-only path.
Store its scoped client configuration/token privately; do not mount the host
control state or child home. Every local write is under explicit `FM_HOME`.

```sh
python3 client.py --config /private/client.json list
python3 client.py --config /private/client.json status team-sandbox
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json send team-sandbox --file /private/request.txt
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json pull
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json collect --interval 10
python3 client.py --config /private/client.json proposals team-sandbox
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json control team-sandbox restart --expected-generation GENERATION
python3 client.py --config /private/client.json diagnostics team-sandbox
python3 client.py --config /private/client.json logs team-sandbox --tail 100 --since-seconds 3600
python3 client.py --config /private/client.json runbook team-sandbox home-usage
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json resources team-sandbox standard --expected-generation GENERATION
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json backup team-sandbox --expected-generation GENERATION
python3 client.py --config /private/client.json backups team-sandbox
python3 client.py --config /private/client.json admin-operation team-sandbox OPERATION_UUID
```

The `send` command records request bytes before networking. For a retry provide
its original `--request-id UUID --retry`; the stored body/correlation are reused.
`--file` remains syntactically required but is not read on retry.
Lifecycle, resource and backup commands likewise persist their exact UUID and
request body under `state/parent-control/control-operations/` or
`admin-operations/` before networking. Re-run lifecycle control with that same
UUID and exact payload, or query `admin-operation` for an administrative action,
after a timeout; never submit a blind replacement.

```sh
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json publish-brain team-sandbox --skill diagnostic-reasoning --model gpt-5.6-sol --effort xhigh
python3 client.py --config /private/operator-client.json approve team-sandbox --file /private/reviewed-approval.json
FM_HOME=/home/nguye/firstmate python3 client.py --config /private/client.json apply APPROVAL_UUID
```

Operator client config still names the target parent ID, but points to the
separate operator token. No approval is generated by these examples.

## Deployment and compatibility gate

This directory contains no installer that mutates a live service. Operator steps:

1. Review/test modules; install root-owned host code/config and private token
   files, plus root-owned mode-0700 `/var/lib/firstmate-control`.
   Install the reviewed `prepare-home-bind.py` at
   `/usr/local/lib/firstmate-control/prepare-home-bind.py`, root-owned mode 0755,
   and verify its published hash before each migration.
   Create `/usr/local/lib/firstmate-control/venv` using `python3 -m venv` and
   install the adjacent `data-access/requirements.txt` using that venv's pip.
   The supplied systemd unit uses that same venv interpreter, so the configured
   data endpoints can load the tested PostgreSQL driver. Keep the venv and all
   installed modules root-owned and outside agent-writable mounts.
2. Select a reachable, restricted listener/TLS configuration. Keep all tokens
   distinct. Place only each scoped token in its authorized container.
   A container reaches `host.docker.internal` only when its Compose service has
   the Linux `host-gateway` mapping. Bind the broker to the exact reachable
private bridge address (the example address is illustrative), or use TLS;
firewall port 8787 from every network except the managed bot networks and
prove an authenticated request from inside MacBot before enabling operations.
Without TLS, the listener accepts only one concrete RFC1918 IPv4 address; it
rejects `0.0.0.0`, public/link-local addresses and hostnames even when private
HTTP is explicitly enabled. Allow only the exact MacBot, Fin and Toan bridge
subnets, then prove a fourth unapproved test network is denied.
   Build each `expected_environment` from the full reviewed Docker
   `.Config.Env`, including image-level variables such as `PATH`; the example is
   illustrative and an omitted or additional live variable is intentional
   identity drift, not something the broker silently accepts.
   Apply bounded `json-file` rotation (`max-size=10m`, `max-file=3`) to both
   managed bots before enabling their administrative manifest; existing
   containers require a reviewed idle-window recreation for this Docker setting.
   Before that recreation, stop the exact old container and run
   `prepare-home-bind.py` against the already seeded mode-0700 child directory.
   Normal Compose uses `create_host_path: false`; never let it synthesize this
   source, and never derive a backup bind source dynamically from an ancestor.
3. Stop child service before running `binding.py` against its existing home,
   as that home's existing numeric owner (not a different root/operator UID).
   This preserves the child user's access to the private marker and manifest.
   The helper verifies its identity, exact previous local marker, and real
   bridge service lock; it refuses live use or conflicting reparenting.
4. Configure child instance with the same parent ID/URL and child ID. The helper
   writes `data/parent-control-binding.json` and the stock supported marker:

   ```text
   schema=fm-secondmate-parent.v1
   route=remote
   ```

   This is truthful cross-filesystem compatibility, not native Herdr metadata.
   Stock `fm_firstmate_root_home` now anchors local crew locks at the child;
   stock outcome publishers use its own `state/parent-replies.status`. The
   runtime drains historical provisioner reports separately and relays current
   child reports to the real parent through the API.
5. Validate `server.py --config ... --check`, then install/review the supplied
   systemd service template. The check initializes/validates local SQLite only;
   it does not probe/start/stop containers or access business data.
6. Run parent list/status, publish one reviewed policy snapshot, enqueue one
   harmless request, verify the actual agent accepts it, returns a correlated
   report, and parent collector durably receives it. Verify its actual Mac DM
   separately. Then verify an ordinary scout can complete and clean up through
   stock report/hold/unlanded-work gates without fabricated parent metadata.

The prior static local seed could validate yet strand cleanup because
`fm-teardown.sh` at the pinned framework checks local parent launch metadata
(`state/team-sandbox.meta`, kind/home) in addition to the seed's marker and
registry. A direct bridge launch never wrote that launch record. The remote
filesystem marker removes only that inapplicable same-filesystem parent-public-
reply lookup. It does not bypass report, hold, PR, worktree or unlanded-work gates.

## Validation

`python3 -m unittest discover -v` runs all offline tests, including the
no-follow exact-home preparer and a real
loopback HTTP parent -> runtime fixture -> child event -> parent inbox roundtrip,
scope denials, idempotency, unknown delivery, immutable proposals, operator-only
approvals, selected-claim application, bounded policy export, generation checks,
and lifecycle command restrictions, durable rename ordering, protected SQLite
paths, and the exact remote-filesystem binding. On Linux, setting
`FM_TEST_FRAMEWORK_ROOT` to the pinned local framework checkout additionally
tests the actual stock root resolver and parent outcome publisher. This test
does not launch an agent. No test contacts Telegram, Docker, GitHub,
Supabase, or production services. Real-container lifecycle, agent acceptance,
stock scout cleanup, and Mac notifications remain explicit deployment checks.

Linux writes fsync each new directory entry and file rename before acknowledging
cursors, notification attempts, and promotion receipts. Windows unit runs check
the ordering but cannot establish Linux filesystem durability. Service startup
rejects writable/symlinked path ancestors and unsafe existing SQLite, WAL, shared
memory, rollback journal, or service-lock files.
