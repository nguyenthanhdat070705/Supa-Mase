# Reproduce the isolated deployment

These are operator steps for the existing Linux deployment and its parent
adapter upgrade. Review and merge the source PR before production rollout.
The local base image and apt packages are not a bit-for-bit image lock.
Use the component guides for current configuration schemas and commands.

## Recorded inputs

| Input | Recorded value |
| --- | --- |
| Framework source | `https://github.com/nguyenthanhdat070705/Supa-Mase.git` |
| Framework commit | `b1ad702fafdd03d94e5ce47cd4ba86e589ad33d6` from `main` |
| Base image | `firstmate:local`, built using the repository's existing `custom/docker` toolchain |
| Recorded local base image ID | `sha256:cf6b2f4688d143ed912b3a68bdcd87f6501b2ae94d79ab8ecfeaf34360cd29e3` (local build ID, not a pullable registry digest) |
| Codex CLI | `0.154.0` |
| Model / effort | `gpt-5.6-sol` / `xhigh` |
| treehouse | `v2.3.0`, Linux x86_64; downloaded binary is intentionally excluded |
| treehouse archive SHA-256 | `94fd2b2c20c35aac1ddc2941317890ad82c9916f5ccecbac4a50cda783eed10f` |
| no-mistakes | `v1.57.0` (`0fcbbff`), standalone executable copied from the trusted primary toolchain |
| no-mistakes binary SHA-256 | `097b329410c898b97cb1253895d4906abe16c09efdcfed0bf2a9708ccac4c549` |
| Auxiliary npm tools | Exact versions in `runtime/Dockerfile` |
| Project / posture | `DemandPlanningMC/demand-planning-maycha`, `direct-PR`, yolo off |
| Seeded application main at initial deployment | `374e51a81618974a5c39cd1f7d9791da900e4618` |
| Git transport | `sandbox-git:team-sandbox-repo`; no upstream token inside the agent |

Fetch the [official treehouse v2.3.0 Linux amd64 archive](https://github.com/kunchenguid/treehouse/releases/download/v2.3.0/treehouse-v2.3.0-linux-amd64.tar.gz)
and verify the archive checksum before extracting its executable to
`runtime/treehouse`. Do not commit the archive or binary. The initial setup also
copied the recorded trusted `no-mistakes` executable only, without its database,
memory or credentials, to private host path `home/.local/bin/no-mistakes` (container `/home/nguye/.local/bin/no-mistakes`). The image PATH includes `/home/nguye/.local/bin`. Verify its binary
checksum when reproducing that step. This source package does not silently
download an unpinned replacement.

## 1. Prepare the host layout and private inputs

The existing installer uses `/home/dat/team-sandbox` as its fixed staging root.
Its `STAGE` and `PUBKEY` constants are explicit reviewed paths, not environment
overrides. Deploy this directory there if using the installer unchanged. Retarget
those constants only through a separately reviewed code change.

The host requires Docker/Compose, OpenSSH server, Python 3.10+, Git, GitHub CLI,
sudo and systemd. `firstmate:local` must exist. Its container must expose the
operator-authorized `DemandPlanningMC` GitHub account to the fixed root-only
credential helper installed by `install-broker.sh`; that helper's captured output
goes only to the host broker. Never run or log the helper interactively.

Keep the source tree and the runtime staging tree distinct. Stage `compose.yml`,
`runtime/`, `git-broker/` and `install-broker.sh` under the fixed deployment root.
Create private `home/` and `secrets/` there, owned appropriately for container UID
1000. Create `secondmate.env` from the example using verified captain ID, forbidden
primary bot ID and an initially empty group list. Put the separate bot token in
`secrets/bot-token` (owner UID 1000, mode 0400); do not put its value in a command,
source file, PR description or shell transcript.

Generate a dedicated Ed25519 SSH client key into the sandbox's private
`home/.ssh/id_ed25519`, with its public key alongside it. Keep the private key
mode 0600. Install a pinned host key in its `known_hosts`, then a private SSH
client config with:

```sshconfig
Host sandbox-git
    HostName host.docker.internal
    User sm-git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Compose supplies `host.docker.internal:host-gateway`; it does not use host
networking. The sandbox receives no unrestricted host SSH key.

## 2. Install and check the host Git broker

Run `install-broker.sh` as root from the reviewed staging layout, then run
`install-broker.sh --check`. The installer verifies SHA-256 hashes of `broker.py`
and its two hooks, refuses conflicting preexisting installation paths, creates
the dedicated `sm-git` account and root-controlled forced command, fetches main,
and validates effective SSH restrictions before reloading SSH. Its credential
helper uses only one fixed no-argument privileged call.

Review [git-broker/README.md](git-broker/README.md) for ownership, retry semantics
and limitations. Verify a clone through the constrained key. Verify arbitrary
SSH commands and protected-branch writes are rejected, without creating
unrequested remote test branches or PRs. Keep the clone's origin set to the
constrained alias.

## 3. Build and seed the separate home

Build the image with `docker compose build secondmate` after supplying the
verified treehouse executable. The image places Codex and helper tools in
`/opt/codex`, outside the separate whole-HOME mount.

Clone the recorded framework revision into private `home/provisioner`. Its
operational project registry must describe `demand-planning-maycha` as direct-PR
with yolo off; clone that project under the controller through the constrained
Git origin. Do not import predecessor backlog or primary private memory.

Run the official helper workflow as container UID 1000 with
`FM_HOME=/home/nguye/provisioner` and current directory at the controller:

```sh
bin/fm-brief.sh team-sandbox --secondmate demand-planning-maycha
# Fill the generated charter brief with this package's charter-domain.md and
# preserve the official generated scaffold/lifecycle instructions.
bin/fm-home-seed.sh team-sandbox /home/nguye/team-sandbox demand-planning-maycha
bin/fm-home-seed.sh validate
```

The child path must not preexist before its initial seed. Normal Compose startup
can create an empty child working directory; seed before normal launch or use a
one-off container with working directory `/home/nguye/provisioner` and an
operator shell entrypoint. Do not run the secondmate service entrypoint while
seeding. Copy `runtime/secondmate-transport.md` into the seeded child's `data/`.
Keep its `.fm-secondmate-home` marker equal to the configured instance identity.
The static provisioner is a seed source only. Before normal startup, use the
reviewed binding helper in [parent-control/](parent-control/README.md) to record
the actual firstmate and configure the stock cross-filesystem `route=remote`
marker. Do not fabricate a local launch record or register this tmux transport
as a stock Herdr session. The former local provisioner marker lacked the launch
metadata required by worker teardown and must be migrated during the upgrade.

## 4. Independent Codex configuration

Provide authentication for the same authorized Codex account as a separate
`home/.codex/auth.json` copy or independent login. Do not mount primary `.codex`,
history, hooks or configuration. Copy the supplied sanitized
`examples/codex.config.toml` to the separate `home/.codex/config.toml`. It records
the model/effort, child project trust and reviewed settings without authentication.

For initial seeding, copy the exported examples to matching filenames in the
child's private `config/` directory. Subsequent approved settings and curated
skills come through the parent adapter's versioned brain export:

- `examples/crew-dispatch.json` to `config/crew-dispatch.json`;
- `examples/crew-harness` to `config/crew-harness`;
- `examples/backend` to `config/backend`;
- `examples/backlog-backend` to `config/backlog-backend`.

The recorded deployment requires every worker to use `xhigh`, with the
secondmate itself fixed to Sol xhigh. Use these exact sanitized files; do not
reconstruct routing from memory or import primary memory/backlog. Review any required framework hook
approvals interactively before accepting Telegram input. Do not copy primary
hook trust or fabricate readiness files to skip this step.

## 5. Install the parent and data adapters

Follow [parent-control/README.md](parent-control/README.md) to install the trusted
host service, fixed parent/child registry, scoped credentials and parent client.
Use a separate credential for every child. Firstmate receives the parent client
and its own credential; it does not receive a Docker socket or host shell key.
Install the collector so proposals and outcomes reach the parent review area.
Configure the captain proposal notification path and test actual receipt.

Follow [data-access/README.md](data-access/README.md) to audit and provision the
restricted database role. This is an explicit operator step, never a startup
side effect. Generate the private table/column manifest after validating RLS,
effective permissions and executable dependencies. Keep database credentials
only on the trusted host. Do not mount the application's administrator `.env`
or a Supabase service-role key into a child.

Create the per-instance manifest and child control-client configuration from
the supplied examples. Match the fixed container, home and identity in the host
registry. Export and synchronize an approved parent configuration/skill snapshot.
Keep the existing child's requested Sol/xhigh setting unless captain changes it.

## 6. Start and verify

Start only the secondmate service with `docker compose up -d secondmate`. The
entrypoint starts `team-sandbox:0.0` on tmux socket `secondmate`; it runs no
primary `fm-up` or primary cron. Its fixed prompt reads the charter and transport
rules and requests a real agent tool call:

```sh
python3 /opt/secondmate/bridge.py ready --proof SOL_XHIGH_SANDBOX_READY
```

Confirm the actual Codex model/effort in local session metadata. Inspect
`python3 /opt/secondmate/bridge.py status` inside the container. A captain DM
should pass through durable intake, actual agent handling, bound Telegram reply,
and completion. A later operator TUI modal can retain the same process identity,
so stop the bridge before interactive maintenance.

For groups, have captain add and enable the bot using the runtime guide's
captain-only commands. The initial environment allowlist is imported once;
subsequent onboarding and revocation use durable runtime state. Keep the
explicitly excluded company group forbidden. Use a fresh request for group/topic
reply verification; ignored onboarding messages are not replayed. Never run
another getUpdates consumer against this bot.

Verify a parent task and correlated child outcome, startup turn completion,
actual Codex input acknowledgment, group/topic reply, group revocation, a
Supabase read and rejected write/SQL request. Create a harmless knowledge proposal
and confirm captain notification and that it remains unapproved. Exercise
promotion with an explicit approval for that exact test proposal and claims.

For an existing child, inspect active workers and queue state first. Reconcile
uncertain deliveries and drain or explicitly resolve work before service
replacement. Preserve the queue, group registry and child memory. Record the
source commit, service version, checks and rollback revision in the private
deployment record. Never reset the live queue to make an upgrade pass.
