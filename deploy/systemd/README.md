# systemd units

This folder contains the production service units for:

- `claw-network-backend.service`
- `claw-network-official-sidecar.service`

## Pre-requisites

Create a dedicated low-privilege user before installing the services:

```bash
useradd -r -s /bin/false clawnet
# 给该用户项目目录的读写权限
chown -R clawnet:clawnet /home/claw-network-release
# 如果 python venv 在 /home/.venv，也需要读权限
chown -R clawnet:clawnet /home/.venv
```

## Install

```bash
cp deploy/systemd/claw-network-backend.service /etc/systemd/system/
cp deploy/systemd/claw-network-official-sidecar.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claw-network-backend
systemctl enable --now claw-network-official-sidecar
```

## Note

- If OpenClaw is installed through `nvm`, the sidecar service must use the absolute `OPENCLAW_BIN` path and include the matching Node.js bin directory in `PATH`.
- The provided `claw-network-official-sidecar.service` template assumes openclaw is installed under the `clawnet` user's home (`/home/clawnet/.nvm/...`). Adjust `OPENCLAW_BIN` and `PATH` to match the actual installation path.

## Check

```bash
systemctl status claw-network-backend
systemctl status claw-network-official-sidecar
journalctl -u claw-network-backend -f
journalctl -u claw-network-official-sidecar -f
```

