# Running and deploying

## Run

Install Docker, then in a clone of this repository:

```bash
cp .env.example .env
$EDITOR .env          # MQTT broker, topic and login; the file explains each setting
docker compose up -d
```

`compose.yml` runs the published image (`ghcr.io/joroses/sigsleuth`, for PCs and ARM boards such as a Raspberry Pi); where none is published, or with `--build`, it builds one from the clone, which takes a few minutes. Set `SIGSLEUTH_IMAGE` in `.env` to run another image. The page is served on `SIGSLEUTH_API_HOST`:`SIGSLEUTH_API_PORT`, `127.0.0.1:8000` by default, and the database lives in the `data` volume.

The page keeps serving saved history whatever capture does. Broker failures are retried, up to two minutes apart; a failed database write pauses capture for five minutes. Only a rejected subscription stops capture until restart. Capture subscribes at QoS 0, so anything missed while it is down is lost. `GET /api/v1/status`, Diagnostics and `docker compose logs -f` show capture's state and skipped deliveries by reason.

There is no channel-key setting: capture reads cleartext payloads and the default Meshtastic key. Custom-key traffic is saved and grouped but not decoded.

To run without Docker, see [development setup](setup.md#run).

## Update

```bash
git pull
docker compose pull && docker compose up -d   # or: docker compose up -d --build
```

Pull the repository too, since `compose.yml` and `.env.example` change with the code. A raised derived-table version rebuilds the derived tables at startup: pages fill in as it goes and Diagnostics shows `rebuilding` (3 to 13 minutes for one to two weeks of a busy subscription, depending on the machine). Capture keeps saving deliveries meanwhile and the rebuild catches up with them; survey uploads get 503 and the phone sends them again later.

## Database reset

A facts version the code cannot upgrade needs a reset; the log says so. Delete the volume and start again:

```bash
docker compose down -v
docker compose up -d
```

Grouping is saved with the facts, so a grouping change applies only to new receptions unless you reset.

## Retention and disk

While capturing, packets older than `SIGSLEUTH_RETENTION_DAYS` (default 14) are deleted about every five minutes (`api` mode deletes nothing), and survey measurements older than `SIGSLEUTH_COVERAGE_RETENTION_DAYS` (default 90). An upload refuses (skips as `too_old`) only measurements past that survey limit, so a phone that was offline for weeks still uploads. There is no size limit: leave disk room for the busiest two weeks (about 1.2 GB of facts for 14 days of a busy regional subscription, plus derived tables and the WAL). The SQLite file stays near its largest size; Diagnostics shows file and data size.

## External services

- The map's basemap style and tiles load directly in the browser; SigSleuth does not proxy them.
- The app fetches terrain tiles from AWS open data to check links 250 km or longer (`terrain.py`). Without them those links show unchecked and are retried; capture does not wait.

## A public server

SigSleuth has no accounts. Anyone who can reach it sees everything it recorded; only Diagnostics and signal survey uploads take a password (HTTP Basic, any user name): `SIGSLEUTH_ADMIN_PASSWORD` for Diagnostics and `SIGSLEUTH_UPLOAD_PASSWORD` for uploads, so surveyors need not get the admin one. Without the first, Diagnostics is open; without the second, uploads are refused. Put it behind a reverse proxy with HTTPS, since Basic sends the password in the clear.

Keep `SIGSLEUTH_API_HOST=127.0.0.1` and let the proxy reach the published port; Docker's published ports bypass firewalls such as ufw, so `0.0.0.0` would also expose the plain-HTTP port. With [Caddy](https://caddyserver.com/), which fetches the certificate itself:

```caddyfile
mesh.example.org {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000
}
```

Compression matters: the default map's 2.7 MB of JSON becomes about 310 KB.
