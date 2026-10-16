# harrisonfd-dispatch

A self-hosted emergency dispatch radio monitor. Captures analog FM transmissions via RTL-SDR, transcribes them with OpenAI Whisper, analyzes them for priority keywords, and serves them through a public-facing web UI with auto-play audio, signal stats, and LLM-powered alert detection.

Live at [harrisonfd.live](https://harrisonfd.live)

---

## Architecture

```
RTL-SDR dongle
    ↓ rtl_fm (capture node)
    ↓ sox (silence-gated WAV segmentation)
    ↓ rsync (push-on-close to media server)
    ↓ PostgreSQL INSERT (metadata + signal stats)

Media server
    ↓ Whisper daemon (inotify → transcribe → UPDATE)
    ↓ LLM service (keyword scan → priority score → alerts)
    ↓ REST API (Flask, port 5001)

Capture node (public-facing)
    ↓ Flask WebUI (port 8080)
    ↓ Nginx (reverse proxy, SSL, rate limiting)
    → harrisonfd.live
```

### Nodes

| Node | Role |
|------|------|
| **Capture node** (OptiPlex 5060, Ubuntu 22.04) | RTL-SDR capture, WAV spool, rsync, Postgres INSERT, WebUI, Nginx |
| **Media server** (Ubuntu) | WAV archive, Whisper transcription, LLM analysis, REST API, PostgreSQL |
| **ariacore** (optional) | Ollama LLM endpoint for priority scoring and alert narratives |

---

## Features

- **Auto-play queue** — new transmissions appear and play automatically in the browser
- **Per-transmission signal stats** — RMS amplitude, max amplitude, rough frequency
- **Keyword alert system** — 40+ seeded keywords across fire, medical, traffic, and police categories; fully editable via REST API at runtime
- **LLM priority scoring** — routine / elevated / high classification with one-sentence summary
- **Public WebUI** — dark scanner aesthetic, mobile-friendly, SSL, rate-limited
- **Disclaimer modal** — one-time dismissable notice on first visit
- **Visitor counter** — today and all-time, CDT midnight reset
- **Standalone tuner tool** — interactive keypress tuning with live Icecast audio stream

---

## Requirements

### Capture node
- Ubuntu 22.04 (or Raspberry Pi OS for Pi deployment)
- RTL-SDR dongle (RTL2832U / R820T tested)
- `rtl-sdr`, `sox`, `python3`, `python3-venv`, `rsync`, `nginx`, `certbot`, `ffmpeg`, `icecast2`

### Media server
- Ubuntu (any recent LTS)
- PostgreSQL 14+
- `python3`, `python3-venv`, `ffmpeg`
- Python packages: `flask`, `psycopg2-binary`, `openai-whisper`, `inotify`, `requests`

---

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/rotarybean/harrisonfd-dispatch.git
cd harrisonfd-dispatch
cp .env.example .env
# Edit .env with your values
```

### 2. Media server — database

```bash
# Create Postgres user and database
sudo -u postgres psql <<EOF
CREATE USER your_db_user WITH PASSWORD 'your_db_password';
CREATE DATABASE dispatch OWNER your_db_user;
GRANT ALL PRIVILEGES ON DATABASE dispatch TO your_db_user;
EOF

# Apply schema (seeds 40+ keyword rules automatically)
psql -U your_db_user -d dispatch -h localhost -f shared/sql/schema.sql
psql -U your_db_user -d dispatch -h localhost -f shared/sql/migrate_v2.sql
psql -U your_db_user -d dispatch -h localhost -f shared/sql/migrate_v3.sql
```

### 3. Media server — Python environment

```bash
sudo useradd -r -m -s /bin/false dispatch
sudo mkdir -p /opt/dispatch/{whisper,api,llm,venv}
sudo -u dispatch python3 -m venv /opt/dispatch/venv
sudo -u dispatch /opt/dispatch/venv/bin/pip install \
    flask psycopg2-binary openai-whisper inotify requests
```

### 4. Media server — deploy and start services

```bash
sudo cp mediaserver/whisper/whisper_daemon.py /opt/dispatch/whisper/
sudo cp mediaserver/api/api.py                /opt/dispatch/api/
sudo cp mediaserver/llm/llm_service.py        /opt/dispatch/llm/

# Edit service files to set your IPs and credentials, then:
sudo cp mediaserver/services/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dispatch-llm dispatch-whisper dispatch-api
sudo systemctl start dispatch-llm dispatch-whisper dispatch-api
```

### 5. Capture node — blacklist DVB driver

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo update-initramfs -u
sudo reboot
```

### 6. Capture node — SSH key for rsync

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dispatch_rsync -N ""
# Authorize the public key on your media server's dispatch user
ssh-copy-id -i ~/.ssh/dispatch_rsync.pub dispatch@your_media_server_ip
```

### 7. Capture node — Python environment

```bash
sudo mkdir -p /var/spool/dispatch /opt/dispatch/{scripts,templates,venv}
python3 -m venv /opt/dispatch/venv
/opt/dispatch/venv/bin/pip install flask psycopg2-binary inotify requests
```

### 8. Capture node — deploy and start services

```bash
sudo cp optiplex/scripts/*.sh  /opt/dispatch/scripts/
sudo cp optiplex/scripts/*.py  /opt/dispatch/scripts/
sudo cp optiplex/webui.py      /opt/dispatch/
sudo cp -r optiplex/templates  /opt/dispatch/
sudo chmod +x /opt/dispatch/scripts/*.sh

# Edit service files, then:
sudo cp optiplex/services/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dispatch-capture dispatch-agent dispatch-webui
sudo systemctl start dispatch-capture dispatch-agent dispatch-webui
```

### 9. Nginx + SSL

```bash
sudo cp optiplex/nginx/harrisonfd /etc/nginx/sites-available/harrisonfd
sudo ln -s /etc/nginx/sites-available/harrisonfd /etc/nginx/sites-enabled/
sudo certbot --nginx -d your.domain.here -d www.your.domain.here
```

---

## RTL-SDR tuning

Finding the right gain and PPM correction for your dongle and location is essential. Use the included interactive tuner:

```bash
sudo systemctl start icecast2
python3 tools/rtl_tuner.py
# Listen at http://your-capture-node-ip:8000/tuner in VLC
```

Key controls: `p/P` PPM, `g/G` gain, `h/H` highpass, `l/L` lowpass, `n/N` notch, `v/V` volume, `S` save settings.

Once satisfied, paste the saved settings from `tuner_settings.txt` into `optiplex/scripts/capture.sh`.

**Common issues:**
- `PLL not locked` in rtl_test — normal for R820T, ignore
- Signal clipping (Maximum amplitude ~0.500 constantly) — gain too high
- No separation between noise and transmission — gain too low or wrong PPM
- Constant tone in audio — try notch filter or `-E dc` flag

---

## REST API

Base URL: `http://your-media-server:5001/api`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Service health + DB status |
| GET | `/stats` | Live counts (today, elevated, high, alerts) |
| GET | `/stats/daily` | Daily summary (up to 90 days) |
| GET | `/transmissions` | Paginated list; filter by `q`, `date`, `priority` |
| GET | `/transmissions/<id>` | Single transmission |
| GET | `/transmissions/<id>/audio` | Stream WAV file |
| GET | `/alerts` | Unacknowledged alerts |
| POST | `/alerts/<id>/acknowledge` | Acknowledge an alert |
| GET | `/keyword-rules` | List all keyword rules |
| POST | `/keyword-rules` | Add a keyword rule |
| PUT | `/keyword-rules/<id>` | Update a keyword rule |
| DELETE | `/keyword-rules/<id>` | Delete a keyword rule |

---

## Keyword rules

Rules are stored in Postgres and take effect immediately — no restart needed.

```bash
# Add a rule
curl -X POST http://your-server:5001/api/keyword-rules \
  -H "Content-Type: application/json" \
  -d '{"keyword":"water rescue","category":"rescue","alert_level":"high"}'

# Disable a rule
curl -X PUT http://your-server:5001/api/keyword-rules/5 \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'
```

---

## LLM integration

Set `LLM_ENABLED=true` in the dispatch-llm service and point `LLM_ENDPOINT` at an Ollama instance. The system degrades gracefully — keyword matching and alert insertion still work if the LLM is unreachable.

---

## Project structure

```
harrisonfd-dispatch/
├── .env.example              # Credential template — copy to .env
├── .gitignore
├── LICENSE
├── README.md
├── docs/
│   └── DEPLOY.md             # Step-by-step deployment guide
├── optiplex/                 # Capture node (OptiPlex 5060 / Ubuntu)
│   ├── scripts/
│   │   ├── capture.sh        # rtl_fm → sox pipeline
│   │   ├── dispatch_agent.py # WAV watcher → rsync → Postgres INSERT
│   │   └── spool_cleanup.sh  # Cron: remove old spooled WAVs
│   ├── templates/            # Jinja2 WebUI templates
│   ├── nginx/harrisonfd      # Nginx site config
│   ├── services/             # systemd service units
│   └── webui.py              # Flask WebUI
├── mediaserver/              # Media/DB server
│   ├── api/api.py            # REST API (Flask, port 5001)
│   ├── whisper/              # Whisper transcription daemon
│   ├── llm/                  # LLM analysis service (port 5002)
│   └── services/             # systemd service units
├── shared/
│   └── sql/                  # PostgreSQL schema + migrations
└── tools/
    └── rtl_tuner.py          # Interactive SDR tuning tool
```

---

## License

MIT — see [LICENSE](LICENSE)

Operated by [@rotarybean](https://github.com/rotarybean)
