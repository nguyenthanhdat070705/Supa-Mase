# Isolated team secondmate

This implementation runs the Maycha team secondmate as a persistent Docker
sidecar using Codex `gpt-5.6-sol` with `xhigh` reasoning and its own
`MaychaFinance_Bot`. It accepts authorized text requests, keeps its own memory
and work, and delivers project changes through `sandbox/*` branches and PRs.
Captain reviews and decides whether to merge. There is no automatic merge or
knowledge promotion path.

The original shell examples in the parent directory remain unchanged. This
directory contains the separate runtime used for the isolated deployment.

## Components

| Path | Purpose |
| --- | --- |
| `compose.yml` | Separate container, home volume, process/resource limits and host Git gateway alias |
| `runtime/Dockerfile` | Derive from `firstmate:local`; keep pinned Codex and helper tools outside the home mount |
| `runtime/entrypoint.sh` | Start one secondmate tmux session and one Telegram poller |
| `runtime/bridge.py` | Authenticated routing, durable SQLite inbox, verified pane delivery, replies and group discovery |
| `runtime/secondmate-transport.md` | Agent readiness, reply/completion protocol and direct team-bot communication rules |
| `git-broker/` | Host-side forced-command Git transport; only sandbox pushes and PR creation |
| `install-broker.sh` | Reviewed fixed-layout host installer and `--check` validation |
| `charter-domain.md` | Domain scope, delegation, separate memory and captain approval boundaries |
| `secondmate.env.example` | Placeholder configuration; all live values stay outside Git |
| `examples/` | Sanitized Codex, crew dispatch/harness, backend and backlog settings exported from the live home |
| `SOURCE-MANIFEST.json` | SHA-256 source inventory and required Git executable modes |

See [reproduction steps](REPRODUCE.md), the [runtime operator guide](runtime/README.md)
and the [Git broker operator guide](git-broker/README.md).

## Isolation and delivery

The agent has its own `/home/nguye/team-sandbox` home, tmux socket and Telegram
queue. The static `/home/nguye/provisioner` clone seeds this home; production
firstmate keeps only a human-readable external inventory and does not
automatically synchronize, route or respawn this service. No primary memory,
Telegram token, GitHub credential store or Docker socket is mounted in the agent.

Captain authority comes from Telegram's verified numeric sender ID. Group
requests require an explicit allowlist. Minimal group metadata can be discovered
only from captain messages; discovery does not grant access. Replies remain
bound to the originating chat, topic and message. Raw Telegram content never
becomes a shell command: the bridge sends a fixed shell-comment inbox pointer to
a verified immutable tmux pane.

The host Git broker retains upstream publishing credentials outside the agent.
The agent receives a dedicated SSH key with one forced command. Only
`DemandPlanningMC/demand-planning-maycha` sandbox branches can be published, with
PRs against `main`. The broker provides no merge, force-push, deletion or arbitrary
SSH command. A local Git push alone is not proof of upstream PR delivery.

## Validation and limits

Run the two offline suites from their respective directories:

```sh
cd runtime
python3 -m unittest -v test_bridge.py
cd ../git-broker
python3 -m unittest -v test_broker.py
```

The suites exercise identity, routing, persistence, delivery races, group
discovery, real local Git ref rules and simulated upstream PR publication. They
do not contact Telegram, GitHub or a deployment host. Real startup additionally
requires an agent tool call proving readiness; process identity is not a general
TUI modal detector. Stop the bridge before interactive terminal maintenance.

SQLite commits preserve received updates and failed work. tmux and Telegram
cannot share a transaction with SQLite, so an ambiguous delivery remains queued
for explicit operator reconciliation instead of automatic replay. Only text
messages are supported. Remote branch protection remains a separate control.

## Source changes

Commit and push source changes on a dedicated branch, open a PR against
`maycha-custom`, and ask Mac to review and merge. Never auto-merge. Publishing
this source package does not change or redeploy the running container.
