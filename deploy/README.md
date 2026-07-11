# VPS deployment notes

Server: 5.181.21.13
SSH user: root
Local SSH key: C:\Users\1111\.ssh\vibecode_vps

Backend path on VPS: /opt/music_backend
System user on VPS: musicbackend
Systemd service: music-backend.service
API port: 8000

Store runtime secrets outside the repository in `/etc/music-backend.env`:

```text
MUSIC_APP_AUTH_TOKEN=...
MUSIC_ADMIN_API_KEY=...
MUSIC_JWT_SECRET=...
```

The file must be owned by `root:musicbackend` with mode `0640`.

Health checks:

```bash
curl http://127.0.0.1:8000/api/health
curl http://5.181.21.13:8000/api/health
```

Useful service commands:

```bash
systemctl status music-backend --no-pager
journalctl -u music-backend -n 100 --no-pager
systemctl restart music-backend
```
