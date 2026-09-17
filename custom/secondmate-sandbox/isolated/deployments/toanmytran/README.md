# ToanMyTran independent bot deployment

This is a separate Compose project, home, network, token set and training
boundary for `toanmytran-bot`. It may share the reviewed immutable runtime image,
but it must not receive `team-sandbox` memory, charter, Telegram token or control
token. Its root-owned broker manifest remains `start_allowed: false` until its
own trainer/captain verifies the identity and activation change.

Stage this directory as `/home/dat/toanmytran`. From the same reviewed source
commit, copy `runtime/` to `/home/dat/toanmytran/runtime` and only
`data-access/data_client.py` to the matching local path. Verify every staged hash
against `SOURCE-MANIFEST.json`; do not copy live state or secrets from the finance
bot. Create private `home/`, `secrets/`, `instance.json` and `secondmate.env`
from the examples. Put only this child's parent-control token and separately
provisioned Telegram token in its UID-1000, mode-0400 secret files. The public
username expected by the template is `ToanMyTran_bot`; verify that a freshly
rotated token resolves to that exact username before activation.

Seed `/home/dat/toanmytran/home/toanmytran-bot` with a trainer-reviewed charter
using the same no-network, parent-home-only seed boundary documented in
`../../REPRODUCE.md`, substituting this child ID and its independently selected
project. The seed directory must end as UID:GID 1000:1000, mode 0700. Then run:

```sh
sudo /usr/local/lib/firstmate-control/prepare-home-bind.py \
  --deployment-root /home/dat/toanmytran \
  --child-id toanmytran-bot \
  --container toanmytran-bot \
  --allow-absent
docker compose config --no-env-resolution
docker compose build secondmate
docker compose create secondmate
```

`create` is intentional: do not start this bot yet. Record the immutable image
ID, complete environment, all mounts with `rprivate` propagation, the exact
`toanmytran_default` network ID and bounded log configuration into the private
broker manifest. Exact username verification, a separately held fresh token and
`start_allowed: false` are three separate start gates.

Activation is a later reviewed transition: rotate the Telegram token and verify
its exact username outside chat, then verify the trainer charter and captain ID.
Keep the broker stopped for the exclusive activation window. In one reviewed
deployment, change Compose to `restart: unless-stopped`, recreate the still
stopped container, and update the root-owned manifest together to
`expected_restart_policy: unless-stopped` and `start_allowed: true`. Restart the
broker, require diagnostics to attest the complete new identity, and only then
use the scoped lifecycle API to start it. This sequence gives the activated bot
24/7 restart behavior without exposing a transient lifecycle endpoint during
recreation. Never mount a Docker socket or the captain/operator token here.
