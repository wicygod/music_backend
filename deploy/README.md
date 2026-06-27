# VPS deployment notes

Server: 5.181.21.13
SSH user: root
Local SSH key: C:\Users\1111\.ssh\vibecode_vps

Backend path on VPS: /opt/music_backend
System user on VPS: musicbackend
Systemd service: music-backend.service
API port: 8000

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
