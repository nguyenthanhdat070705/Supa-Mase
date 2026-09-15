# Persistent team-sandbox deployment

This bundle runs the user-authorized secondmate with its own Telegram bot and
`gpt-5.6-sol` / `xhigh`. It is an isolated Docker sidecar. It uses neither primary
`fm-up` nor primary cron, no-mistakes state, GitHub credentials or tmux sockets.

## Host layout

```
secondmate/
  compose.yml
  secondmate.env                 # verified public IDs; mode 600
  secrets/bot-token               # own Telegram token; owner 1000, mode 400
  runtime/                       # this bundle, read-only container mount
    Dockerfile
    treehouse                    # reviewed/pinned Linux binary, operator supplies
    bridge.py
    entrypoint.sh
    secondmate-transport.md
  home/                          # independent persistent volume, owner 1000
    .codex/auth.json              # separately copied auth only, never shared mount
    .codex/config.toml            # separate config/trust, no primary hooks
    provisioner/                 # static pinned distro clone, local seed controller
    team-sandbox/                # persistent child seeded by official local helper
      .fm-secondmate-home        # exact contents: team-sandbox
      data/charter.md
      data/secondmate-transport.md
      state/secondmate-telegram/
```

The official `fm-home-seed.sh` seeds the local child from the provisioner with
the `demand-planning-maycha` project using direct-PR delivery. Provisioning and
charter assembly are separate from this runtime.
Copy `secondmate-transport.md` into the child's `data/`. Main firstmate may keep a
human-readable external-sandbox inventory record; do not register a local active
route into a container home that it cannot supervise. It must not auto-sync or
auto-respawn this external service.

The derived image moves the existing Codex installation to `/opt/codex` so a
whole-HOME mount cannot hide it. Auxiliary npm tool versions are pinned in the
Dockerfile. Supply the operator-reviewed `treehouse` binary before building.
Any Git transport credential must be separate and constrained to sandbox branch
operations; no primary GitHub token or host Docker socket belongs in this image.

## Startup and real readiness check

1. Seed and validate child; set model/config and explicit isolated project trust.
   Install independent Codex auth and the separate bot secret without printing it.
2. Configure verified numeric captain ID, allowed group IDs, expected bot username
   and forbidden primary bot ID. Leave group list empty until authorized/verified.
3. Build/start with `docker compose up -d --build` from the host deployment folder.
4. Inspect with `docker exec secondmate tmux -L secondmate capture-pane -p -t
   team-sandbox:0.0`. The exact target is `team-sandbox:0.0`; `team-sandbox:0` is the
   corresponding single-pane window.
5. Codex receives a fixed startup prompt. The agent reads its charter/transport
   rules and makes a real shell tool call to `bridge.py ready --proof
   SOL_XHIGH_SANDBOX_READY`. The command rejects an operator shell: its ancestry
   must contain the launched agent PID, start time, home and nonce, and the pane
   foreground must be Codex or its positively verified npm Node wrapper. The
   bridge withholds Telegram pointers during the initial unready trust/auth flow.
6. Verify the readiness result and the actual model/effort in the startup UI or
   local Codex session metadata. Readiness proves a real agent tool call, not a
   provider-side model identity attestation. If startup is blocked, resolve that
   first; do not manufacture readiness JSON.

Readiness is **not a general TUI modal detector**. A later manual selector/auth
dialog can retain the same process identity. Keep the agent pane dedicated and
unattended while the bridge is live. Stop the bridge before interactive operator
maintenance and re-establish agent readiness before resuming it.

## Transport behavior

- Captain identity uses Telegram `from.id` in captain's private DM or an allowed
  group. Unknown chats, bot senders, anonymous admins/channel senders and non-text
  updates are ignored and recorded without retaining their content.
- Each accepted update and the next Telegram cursor commit in one SQLite
  transaction. Duplicate updates never replace earlier queue state.
- A single flock-protected poller writes canonical JSON inbox files. Only a
  fixed shell-comment pointer (`# SECONDMATE_INBOX`) with a numeric update ID
  enters tmux. User text, names and
  titles never enter shell command text. Wrong marker/PID/start/home/nonce,
  a shell foreground, stale readiness, or a foreground tool causes refusal.
- Paste and Enter address the validated immutable `%pane` ID. Identity is checked
  again before paste and after paste before Enter. This narrows process races;
  tmux and process checks are not one mathematically atomic operation. The marker
  is a harmless shell comment if the last moment of a race reaches a shell.
- Exactly one inbox item may be outstanding. A verified agent readiness proof
  is consumed for delivery. The agent sends `notify --update ID --file PATH`,
  which binds the response to the original chat/topic/message, then
  `complete --update ID`, which requires a successful reply and real agent
  ancestry before enabling the next delivery.
- `notify --captain --file PATH` is the explicit background escalation route.
  Stock parent status lines are also collected from the static provisioner's
  `state/team-sandbox.status` and forwarded to captain via a durable byte cursor.
  Partly appended lines are not acknowledged. Reports stay local if sending fails.
- Outbound destinations are constrained to captain or currently allowed groups;
  there is no freeform chat-ID option on the CLI. No terminal scraping is used
  to guess assistant answers.

## Failure recovery

`docker exec secondmate python3 /opt/secondmate/bridge.py status` reports cursor,
queue counts, report counts and presence of readiness. Presence alone does not
prove readiness is still valid; the poller revalidates every delivery.

For group onboarding, status also exposes `pending_group_candidates`: minimal
group ID/title and sender/message/update IDs observed only when the exact verified
captain sends a message in a group that is not allowlisted. No raw message text is
retained for these ignored updates. Other users, bots and anonymous senders cannot
create candidates. Candidates confer no authority and receive no delivery or
automatic enrollment; an operator must explicitly verify and add the intended
group to `SM_TEAM_GROUP_IDS`. Earlier ignored requests are not replayed.

Messages remain durable when the agent is offline. A crash around tmux delivery
leaves `delivering` or `uncertain`, blocking automated replay. A crash around
Telegram report send leaves `sending` or `uncertain`. Telegram sendMessage and
tmux paste do not provide transactionally coupled idempotency, so exactly-once
delivery across a process/network crash cannot be guaranteed. The bridge chooses
manual reconciliation rather than automatically causing duplicate work/messages.
An operator must inspect the inbox file, actual agent turn and destination before
changing an uncertain row. Never reset the cursor or delete the SQLite database
to recover a stuck item. Back up the DB plus WAL consistently before repair.

The launch command uses the CLI's approval/sandbox bypass because the entire
agent operates inside this separately mounted container. Container isolation
does not replace remote repository branch protection. No auto-merge or automatic
knowledge promotion is authorized by bridge role labels.

## Offline verification

From `runtime/`, run `python3 -m unittest -v test_bridge.py`. The tests never call
live Telegram. They cover captain-in-group identity, sender spoofing, topic/reply
binding, unknown chats, duplicates, durable failed delivery, single outstanding
message, shell rejection, literal input pointers, bot binding, outbound allowlist
and durable parent report forwarding. Linux runtime additionally verifies real
tmux/proc ancestry during the startup smoke check.
