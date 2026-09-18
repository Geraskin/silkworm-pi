FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ARG USERNAME=alexey
ARG USER_UID=1000
ARG USER_GID=1000

# Rename the default 'vscode' user to the desired username.
RUN groupmod --gid ${USER_GID} --new-name ${USERNAME} vscode \
    && usermod \
    --uid ${USER_UID} \
    --gid ${USER_GID} \
    --login ${USERNAME} \
    --home /home/${USERNAME} \
    --move-home \
    vscode \
    && if [ -f /etc/sudoers.d/vscode ]; then mv /etc/sudoers.d/vscode /etc/sudoers.d/${USERNAME}; fi \
    && echo "${USERNAME} ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME}

# SSH client + rsync so the container can deploy to the Raspberry Pi over SSH.
# Remove stale yarn repo (expired GPG key in base image causes apt-get update to fail).
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    rsync \
    && rm -rf /var/lib/apt/lists/*

# App dependencies: Flask web server; gpiozero for the lamp driver on the Pi
# (import fails gracefully when running outside the Pi).
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install \
    flask \
    gpiozero

USER ${USERNAME}
WORKDIR /workspaces/silkworm-pi
