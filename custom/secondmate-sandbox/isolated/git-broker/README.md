# Secondmate Git / PR broker

This trusted **host-side** bundle lets the isolated secondmate push `sandbox/*` branches and receive a GitHub PR link. The agent receives an SSH private key with one forced command. It never receives the upstream GitHub credential. Captain reviews and merges manually; this broker implements no merge operation.

Fixed upstream: `DemandPlanningMC/demand-planning-maycha`, PR base `main`. Changing these requires an operator code change. No files here contain a credential. Local tests make no network calls.

## Trust boundary and prerequisites

- Linux host with Python 3.10+, OpenSSH, Git, GitHub CLI, and a **dedicated Unix broker account** (examples use `sm-git`). Do not run it under firstmate's or secondmate's UID.
- Agent container must not mount the broker repository, state, SSH server filesystem, config, Docker socket, or credentials. Mount only its own client SSH key and known_hosts into the agent.
- Install `broker.py` and its parent directories root-owned and not writable by the broker or agent. Run Python with `-I` as shown, ignoring Python startup environment.
- Config path is fixed at `/etc/secondmate-git-broker/config.json`; it must be root-owned and not group/other writable. Copy `config.example.json` and adapt only operator paths/credential argv. The program rejects environment-based configuration.
- `credential_command` is a fixed argv array, never shell-evaluated. Its trusted wrapper returns **one GitHub token line on stdout to this broker only**; stdout/stderr are captured and never relayed. This IPC is intentional. Do not invoke that credential wrapper interactively or log it. Do not give this account general `sudo` or Docker-group access. If privilege is needed, allow only one immutable no-argument token wrapper in sudoers, using e.g. `['/usr/bin/sudo', '-n', '/usr/local/libexec/secondmate-git-token']` in the config.
- Token exists transiently in the broker and Git/gh child process environments. It is absent from command argv, generated files, status records, and client-visible logs. Root/the trusted broker UID can inspect their own processes; these privileges must stay outside the agent container.

## Operator installation

1. Provision the dedicated account with a usable login shell for OpenSSH forced commands, **no password login**, and no unrelated authorized keys. Use an SSH `Match User sm-git` block to set `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitTTY no`, `AllowTcpForwarding no`, `X11Forwarding no`, `PermitTunnel no`, `PermitUserEnvironment no`; do not accept client-controlled environment variables for this account. Disable `~/.ssh/rc` via the `restrict` key option and keep `.ssh` / `authorized_keys` operator-controlled. `restrict` requires a sufficiently recent OpenSSH.
2. Install `broker.py` at `/usr/local/libexec/secondmate-git-broker/broker.py`, mode 0755, root-owned. Install the fixed config at the path above, mode 0644 or root:broker 0640. Its parent directories and credential helper must also be operator-controlled.
3. Initialize `/var/lib/secondmate-git-broker/repo.git` as a bare repository and seed the approved main source/history through an operator-controlled read/pull process. Give `sm-git` write access to `objects/`, `refs/`, and `logs/` if used. Keep the repository root, `config`, `HEAD`, `hooks/`, and `packed-refs` **root-owned and not writable by sm-git**; otherwise a directory owner could replace trusted hooks. Parent directories must not be broker-writable. Do not run `git gc`/`pack-refs` as the broker; operator maintenance must preserve these ownership rules. The broker adds this exact repository to Git `safe.directory` for each invocation.
4. Configure the bare repo with `receive.denyDeletes=true`, `receive.denyNonFastForwards=true`, `receive.fsckObjects=true`, `transfer.fsckObjects=true`, `receive.autogc=false`, and `gc.auto=0`. Install the supplied executable `pre-receive` and `post-receive` hooks into its root-owned `hooks/`. Ensure there are no additional unreviewed hooks, remotes, URL rewrites, credential helpers, filters, or Git configuration. Keep `extensions.worktreeConfig` disabled. The hooks use the installed fixed broker path.
5. Create `/var/lib/secondmate-git-broker/state` as mode 0700, owned by `sm-git`; it contains status JSON and a publication lock, never GitHub credentials.
6. Generate a dedicated client key through the operator's normal provisioning flow. Add its public key using the exact `authorized_keys.example` prefix. Do not add a second unrestricted copy. Pin the SSH host key in the sandbox's `known_hosts`; do not disable host-key checking.
7. Provide the sandbox a Git remote such as `sm-git@BROKER_HOST:team-sandbox-repo`, using only its dedicated key (`IdentitiesOnly yes`). Clone/fetch this alias. Push with `git push origin HEAD:refs/heads/sandbox/<task>`. Any other path, shell command, extra Git argument, main/tag/ref update, deletion, or non-fast-forward update is rejected.

Operator installation must also ensure sufficient disk capacity and network access only where needed. This is a Git/PR transport, not a general SSH shell, Git LFS service, or knowledge promotion mechanism.

## Publish semantics and retries

- `pre-receive` validates the entire update batch before accepting any ref. Sandbox refs must point to commits; updates must be fast-forward. Hooks preserve Git's quarantine object environment only while validating newly received objects.
- After local acceptance, `post-receive` serializes publications with `flock`, resolves the current validated sandbox ref, pushes that exact commit to the **same** upstream sandbox ref without force, then finds or creates an open PR against `main`.
- A failed upstream push never proceeds to PR creation. A failed PR step preserves `stage: upstream_pushed`. Successful retries are idempotent: push is up to date and an existing open PR is reused. Concurrent external PR creation is handled by another lookup. A branch with no diff from `main` may have no PR to create; this reports a pending PR step instead of success.
- **Git cannot reject a locally accepted push from `post-receive`.** The transport may return success even when publication fails. Read the explicit `BROKER:` result and status JSON; only `pr_ready` confirms a PR. There is no cross-repository atomic transaction, rollback, or automatic retry daemon.
- The operator can retry the latest local head: `sudo -u sm-git /usr/bin/python3 -I /usr/local/libexec/secondmate-git-broker/broker.py publish refs/heads/sandbox/<task>`. This operation is deliberately unavailable to the SSH client. Status is written atomically as `<sha256(ref)>.json` in the state directory. Retrying after a crash or ambiguous network result is safe.
- Protect upstream `main` with captain-controlled rules/review requirements as an independent guard. The broker has no `gh pr merge`, upstream main push, branch delete, or knowledge-copy route. It cannot constrain what credentials or other tools are separately installed inside the agent; those must remain absent.

## Offline verification

Run `python -m unittest -v test_broker.py` in this directory. Tests use temporary local bare repos for real ref and ancestry checks, simulate Git/GitHub publishing subprocesses, reject forced-command injection, verify retry stages, and ensure a fake credential is absent from argv/status/client output. Tests do not contact GitHub, Telegram, or the server. Production OpenSSH, Linux filesystem ownership, and container integration still require operator verification.
