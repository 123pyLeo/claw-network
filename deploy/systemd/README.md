# systemd units

This folder contains the production service units for:

- `claw-network-backend.service`
- `claw-network-official-sidecar.service`

Install:

```bash
cp deploy/systemd/claw-network-backend.service /etc/systemd/system/
cp deploy/systemd/claw-network-official-sidecar.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claw-network-backend
systemctl enable --now claw-network-official-sidecar
```

Check:

```bash
systemctl status claw-network-backend
systemctl status claw-network-official-sidecar
journalctl -u claw-network-backend -f
journalctl -u claw-network-official-sidecar -f
```
