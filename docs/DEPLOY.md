# Deployment Guide

## Prerequisites

- Two Linux hosts: a **capture node** (any x86 Ubuntu/Debian machine with a USB port) and a **media server** (Ubuntu)
- RTL-SDR dongle (RTL2832U / R820T tested)
- Domain name pointed at your capture node's public IP
- Ports 80 and 443 forwarded to the capture node on your router

---

## Step 1 — Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

Key values to set:
- `PGHOST` — IP of your media server
- `PGUSER` / `PGPASSWORD` — Postgres credentials
- `MEDIA_SERVER_IP` — IP of your media server
- `SSH_KEY` — path to SSH key for rsync (generated in Step 4)
- `RTL_PPM` — your dongle's PPM correction (find with `tools/rtl_tuner.py`)
- `RTL_GAIN` — your dongle's optimal gain
- `SITE_DOMAIN`, `CONTACT_EMAIL`, `BMAC_URL` — site branding

---

## Step 2 — Media server: database

```bash
sudo -u postgres psql <<EOF
CREATE USER your_db_user WITH PASSWORD 'your_password';
CREATE DATABASE dispatch OWNER your_db_user;
GRANT ALL PRIVILEGES ON DATABASE dispatch TO your_db_user;
EOF

psql -U your_db_user -d dispatch -h localhost -f shared/sql/schema.sql
```

---

## Step 3 — Media server: Python environment

```bash
sudo useradd -r -m -s /bin/false dispatch
sudo mkdir -p /opt/dispatch/{whisper,api,llm,venv}
sudo chown -R dispatch:dispatch /opt/dispatch
sudo mkdir -p /data/dispatch
sudo chown dispatch:dispatch /data/dispatch

sudo -u dispatch python3 -m venv /opt/dispatch/venv
sudo -u dispatch /opt/dispatch/venv/bin/pip install \
    flask psycopg2-binary openai-whisper inotify requests
```

---

## Step 4 — Media server: deploy and start

```bash
sudo cp mediaserver/whisper/whisper_daemon.py /opt/dispatch/whisper/
sudo cp mediaserver/api/api.py                /opt/dispatch/api/
sudo cp mediaserver/llm/llm_service.py        /opt/dispatch/llm/
sudo cp .env                                  /opt/dispatch/.env
sudo chown -R dispatch:dispatch /opt/dispatch

sudo cp mediaserver/services/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dispatch-llm dispatch-whisper dispatch-api
sudo systemctl start dispatch-llm
sleep 3
sudo systemctl start dispatch-whisper dispatch-api

# Verify
curl http://localhost:5001/api/health
curl http://localhost:5002/internal/health
```

---

## Step 5 — Capture node: blacklist DVB driver

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo update-initramfs -u
sudo reboot
# After reboot: rtl_test -t should open the dongle cleanly
```

---

## Step 6 — Capture node: SSH key for rsync

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dispatch_rsync -N ""
# Authorize on media server:
ssh-copy-id -i ~/.ssh/dispatch_rsync.pub dispatch@your_media_server_ip
# Test:
ssh -i ~/.ssh/dispatch_rsync dispatch@your_media_server_ip echo OK
```

Update `SSH_KEY` in your `.env`.

---

## Step 7 — Capture node: install packages

```bash
sudo apt install -y rtl-sdr sox python3 python3-venv rsync \
                    nginx certbot python3-certbot-nginx ffmpeg icecast2
```

---

## Step 8 — Capture node: Python environment

```bash
sudo mkdir -p /var/spool/dispatch /opt/dispatch/{scripts,templates,venv}
sudo chown -R $USER:$USER /opt/dispatch /var/spool/dispatch

python3 -m venv /opt/dispatch/venv
/opt/dispatch/venv/bin/pip install flask psycopg2-binary inotify requests
```

---

## Step 9 — Capture node: tune RTL-SDR

Before deploying services, find your dongle's correct PPM and gain:

```bash
sudo systemctl start icecast2
python3 tools/rtl_tuner.py
# Open http://your-capture-node:8000/tuner in VLC
# Adjust p/P (PPM) and g/G (gain) until voice sounds clear
# Press S to save settings
```

Update `RTL_PPM`, `RTL_GAIN` in `.env`.

---

## Step 10 — Capture node: deploy and start

```bash
sudo cp optiplex/scripts/*.sh  /opt/dispatch/scripts/
sudo cp optiplex/scripts/*.py  /opt/dispatch/scripts/
sudo cp optiplex/webui.py      /opt/dispatch/
sudo cp -r optiplex/templates  /opt/dispatch/
sudo cp .env                   /opt/dispatch/.env
sudo chmod +x /opt/dispatch/scripts/*.sh

sudo cp optiplex/services/*.service /etc/systemd/system/
# Edit each service file: replace YOUR_USERNAME with your actual username
sudo systemctl daemon-reload
sudo systemctl enable dispatch-capture dispatch-agent dispatch-webui
sudo systemctl start dispatch-capture dispatch-agent dispatch-webui

# Crontab for spool cleanup:
# crontab -e
# */30 * * * * /bin/bash /opt/dispatch/scripts/spool_cleanup.sh >> /var/log/dispatch-cleanup.log 2>&1
```

---

## Step 11 — Nginx + SSL

```bash
# Edit optiplex/nginx/harrisonfd — replace YOUR_DOMAIN
sudo cp optiplex/nginx/harrisonfd /etc/nginx/sites-available/harrisonfd
sudo ln -s /etc/nginx/sites-available/harrisonfd /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Add rate limit zone to /etc/nginx/nginx.conf inside http {}:
# limit_req_zone $binary_remote_addr zone=dispatch:10m rate=30r/m;

sudo nginx -t
sudo systemctl start nginx
sudo certbot --nginx -d your.domain -d www.your.domain

# Auto-renew cron:
# 0 3 * * * certbot renew --quiet
```

---

## Step 12 — Firewall (media server)

```bash
sudo ufw allow 22
sudo ufw allow 8096   # Jellyfin or other services
sudo ufw allow from your_capture_node_ip to any port 5001
sudo ufw deny 5001
sudo ufw allow from 192.168.x.0/24 to any port 5432
sudo ufw allow from 192.168.x.0/24 to any port 139
sudo ufw allow from 192.168.x.0/24 to any port 445
sudo ufw --force enable
```

---

## Verify end-to-end

```bash
# Capture node
sudo systemctl status dispatch-capture dispatch-agent dispatch-webui

# Media server
sudo systemctl status dispatch-whisper dispatch-llm dispatch-api
curl http://localhost:5001/api/health

# Public site
curl -I https://your.domain
```

Wait for a transmission. Watch:
```bash
journalctl -u dispatch-agent -f   # on capture node
journalctl -u dispatch-whisper -f # on media server
```

A successful capture looks like:
```
[agent] INFO WAV closed: harrisonfr_20260524_143022.wav
[agent] INFO rsync OK → /data/dispatch/2026/05/24/
[agent] INFO Inserted transmission #1: harrisonfr_20260524_143022.wav (8.3s)
```
Followed ~30s later on the media server:
```
[whisper] INFO Transcribing harrisonfr_20260524_143022.wav …
[whisper] INFO  → Engine 1 responding to structure fire on...
```
