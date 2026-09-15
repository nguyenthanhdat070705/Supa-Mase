FROM firstmate:local
USER root
# Whole-HOME volume must not hide the image's pinned Codex installation.
RUN cp -a /home/nguye/.npm-global /opt/codex \
 && apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/codex/bin:/home/nguye/.local/bin:/usr/local/bin:/usr/bin:/bin
RUN npm install --prefix /opt/codex --global \
      tasks-axi@0.2.5 quota-axi@0.1.43 gh-axi@0.1.35 \
      chrome-devtools-axi@0.1.34 lavish-axi@0.1.67
# Operator places the reviewed pinned Linux treehouse binary beside Dockerfile.
COPY --chmod=0755 treehouse /usr/local/bin/treehouse
USER 1000:1000
WORKDIR /home/nguye/team-sandbox
ENTRYPOINT ["/bin/bash", "/opt/secondmate/entrypoint.sh"]
