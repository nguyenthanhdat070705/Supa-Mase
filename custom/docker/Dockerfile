# firstmate — reproducible toolchain image (target: Linux server 24/7)
# State + secrets are NOT baked in; they arrive via mounted volumes (see docker-compose.yml).
# Build:  docker compose build
FROM ubuntu:24.04

ARG NODE_MAJOR=22
ARG CODEX_VERSION=0.154.0
ARG USER=nguye
ARG UID=1000
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Ho_Chi_Minh

# --- system toolchain (pinned major deps; matches the current WSL host) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git tmux jq python3 python3-pip python3-venv \
      ripgrep tzdata unzip xz-utils procps util-linux less nano \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# --- Node.js (NodeSource) ---
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# --- GitHub CLI (official apt repo) ---
RUN mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# --- supercronic: container-native cron that runs as the non-root user ---
ARG SUPERCRONIC_VERSION=v0.2.33
RUN ARCH=$(dpkg --print-architecture) \
 && case "$ARCH" in amd64) SC_ARCH=amd64 ;; arm64) SC_ARCH=arm64 ;; *) SC_ARCH=amd64 ;; esac \
 && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${SC_ARCH}" -o /usr/local/bin/supercronic \
 && chmod +x /usr/local/bin/supercronic

# --- non-root user matching the host (UID 1000 = nguye) ---
RUN (id -u $USER >/dev/null 2>&1 || useradd -m -u $UID -s /bin/bash $USER) \
 && mkdir -p /home/$USER/.local/bin /home/$USER/.npm-global \
 && chown -R $USER:$USER /home/$USER
USER $USER
WORKDIR /home/$USER
ENV HOME=/home/$USER \
    PATH=/home/$USER/.npm-global/bin:/home/$USER/.local/bin:/usr/local/bin:/usr/bin:/bin

# --- Codex CLI (npm, user prefix — matches the host's post-fix layout) ---
RUN npm config set prefix /home/$USER/.npm-global \
 && npm install -g @openai/codex@${CODEX_VERSION}

# --- Claude Code (official native installer -> ~/.local) ---
RUN curl -fsSL https://claude.ai/install.sh | bash

# no-mistakes is a static binary that travels in the ~/.no-mistakes volume (state),
# so it is intentionally NOT installed here — it arrives with its sqlite + repos.

COPY --chown=nguye:nguye entrypoint.sh /home/$USER/entrypoint.sh
RUN chmod +x /home/$USER/entrypoint.sh

ENTRYPOINT ["/home/nguye/entrypoint.sh"]
