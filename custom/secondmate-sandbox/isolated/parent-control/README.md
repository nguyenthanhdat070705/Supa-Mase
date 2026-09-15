# Explicit container parent adapter

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
| `server.py` | Authenticated API, fixed runtime dispatch, durable request/event/proposal/approval SQLite records |
| `lifecycle.py` | Existing-container identity/generation checks, runtime quiesce lease, start/stop/restart/resume |
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
never started again. The service does not create containers.

Only a separately configured operator with `allow_recovery: true` may supply
`operator_recovery_reference` to recover an active or unverified runtime.
That explicit recovery may interrupt work, but never removes volumes or work.
The operation journal preserves executing/unknown results instead of blindly
repeating maintenance. A crash can leave intake quiesced: inspect status and
use `resume` if no stop/restart completed. There is no autonomous recovery loop.

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
```

The `send` command records request bytes before networking. For a retry provide
its original `--request-id UUID --retry`; the stored body/correlation are reused.
`--file` remains syntactically required but is not read on retry.

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
   Create `/usr/local/lib/firstmate-control/venv` using `python3 -m venv` and
   install the adjacent `data-access/requirements.txt` using that venv's pip.
   The supplied systemd unit uses that same venv interpreter, so the configured
   data endpoints can load the tested PostgreSQL driver. Keep the venv and all
   installed modules root-owned and outside agent-writable mounts.
2. Select a reachable, restricted listener/TLS configuration. Keep all tokens
   distinct. Place only each scoped token in its authorized container.
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

`python3 -m unittest -v test_control.py test_binding.py test_storage.py` runs offline tests, including a real
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
