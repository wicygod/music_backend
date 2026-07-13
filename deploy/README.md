# VPS deployment notes

Server: 5.181.21.13
SSH user: root
Local SSH key: C:\Users\1111\.ssh\vibecode_vps

Backend path on VPS: /opt/music_backend
System user on VPS: musicbackend
Systemd service: music-backend.service
Internal API port: 8000 (loopback only; publish through HTTPS reverse proxy)

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
```

The service stores mutable data outside the Git checkout:

- database: `/var/lib/music-backend/music_catalog.db`;
- audio cache: `/var/cache/music-backend/audio`;
- private local backups: `/var/backups/music-backend`.

Before installing the updated unit on an existing server, stop the service and
copy the current database once:

```bash
systemctl stop music-backend
install -d -o musicbackend -g musicbackend -m 0750 /var/lib/music-backend
cp /opt/music_backend/music_catalog.db /var/lib/music-backend/music_catalog.db
chown musicbackend:musicbackend /var/lib/music-backend/music_catalog.db
systemctl daemon-reload
systemctl start music-backend
```

Do not expose Uvicorn directly. Terminate TLS in Caddy/Nginx and allow external
traffic only on port 443. Database backups contain private user data and must
never be committed or pushed to Git; copy them only to encrypted private storage.

Useful service commands:

```bash
systemctl status music-backend --no-pager
journalctl -u music-backend -n 100 --no-pager
systemctl restart music-backend
```
