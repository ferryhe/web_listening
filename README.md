# web_listening

Monitor websites for changes, download documents, expose normalized snapshots, and generate AI summaries.

## Features

- **Website monitoring** – crawl sites, compute content diffs, detect new links and documents
- **Normalized snapshots** – store raw HTML, cleaned HTML, Markdown, fit-Markdown, and fetch metadata for agent-friendly consumption
- **Document downloading** – fetch PDFs, DOCX, XLSX and save locally; content conversion is handled by the separate `doc_to_md` module
- **Document handoff state** – keep `content_md` write-back fields so an external agent or `doc_to_md` pipeline can populate converted Markdown later
- **AI analysis** – summarise weekly changes with OpenAI (falls back to local summary)
- **SQLite storage** – lightweight, no external DB required
- **CLI** – `web-listening` command with rich terminal output
- **REST API** – FastAPI server for programmatic access

## Installation

```bash
pip install -e .
```

Requires Python ≥ 3.10.

## Configuration

For local development, the standard approach is to put non-secret defaults and local secrets in a project-level `.env` file.
This project already supports that via Pydantic Settings.

Use `.env.example` as the template:

```bash
cp .env.example .env
```

Then set your real API key in `.env`:

```dotenv
WL_OPENAI_API_KEY=your_real_api_key
```

`.env` is already ignored by git, so the secret stays local.
For production or shared deployments, prefer real environment variables or a secret manager instead of committing or distributing `.env` files.

All settings can be overridden via environment variables (prefix `WL_`) or a `.env` file:

| Variable | Default | Description |
|---|---|---|
| `WL_DATA_DIR` | `./data` | Root data directory |
| `WL_DB_PATH` | `./data/web_listening.db` | SQLite database path |
| `WL_DOWNLOADS_DIR` | `./data/downloads` | Downloaded documents directory |
| `WL_OPENAI_API_KEY` | *(empty)* | OpenAI API key for AI summaries |
| `WL_OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `WL_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `WL_USER_AGENT` | `web-listening-bot/1.0` | HTTP User-Agent header |
| `WL_REQUEST_TIMEOUT` | `30` | HTTP request timeout (seconds) |

## CLI Usage

### Add a site to monitor

```bash
web-listening add-site https://example.com --name "Example" --tags "news,tech"
```

### List monitored sites

```bash
web-listening list-sites
web-listening list-sites --all   # include inactive
```

### Check sites for changes

```bash
web-listening check                   # check all active sites
web-listening check --site-id 1       # check a specific site
```

### View recorded changes

```bash
web-listening list-changes
web-listening list-changes --site-id 1
web-listening list-changes --since 2024-01-01
```

### Download documents

```bash
# Download all document links found in latest snapshot
web-listening download-docs --site-id 1 --institution "MyOrg"

# Download a specific URL
web-listening download-docs --site-id 1 --institution "MyOrg" --url https://example.com/report.pdf
```

### List downloaded documents

```bash
web-listening list-docs
web-listening list-docs --institution "MyOrg"
```

### Run AI analysis

```bash
web-listening analyze                         # last 7 days
web-listening analyze --since 2024-01-01
```

### Start the API server

```bash
web-listening serve
web-listening serve --host 127.0.0.1 --port 9000
```

## REST API

Start the server with `web-listening serve`, then browse to `http://localhost:8000/docs` for interactive Swagger UI.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/sites` | List active sites |
| `POST` | `/api/v1/sites` | Add a site |
| `GET` | `/api/v1/sites/{id}` | Get site details |
| `GET` | `/api/v1/sites/{id}/snapshots/latest` | Get the latest normalized snapshot for a site |
| `DELETE` | `/api/v1/sites/{id}` | Deactivate a site |
| `POST` | `/api/v1/sites/{id}/check` | Queue a check (background) |
| `POST` | `/api/v1/sites/{id}/download-docs` | Queue document download |
| `GET` | `/api/v1/changes` | List changes (filter by `site_id`, `since`) |
| `GET` | `/api/v1/documents` | List documents (filter by `institution`, `site_id`) |
| `PATCH` | `/api/v1/documents/{id}/content` | Write converted Markdown back to a downloaded document |
| `POST` | `/api/v1/analyze` | Run analysis and store report |
| `GET` | `/api/v1/analyses` | List analysis reports |

### Downstream document conversion

This project still treats document conversion as an external concern.

Recommended flow:

1. `web_listening` downloads the source file and records document metadata
2. an external AI agent or `doc_to_md` converts the file into Markdown
3. the converted Markdown is written back through `PATCH /api/v1/documents/{id}/content`

This keeps `web_listening` focused on tracking, evidence, and retrieval while allowing conversion quality to evolve independently.

## Production Deployment

The recommended production shape for this project is:

- one Linux host
- one Python virtualenv
- one `uvicorn` process
- one local SQLite database on persistent disk
- one reverse proxy such as Nginx
- scheduled CLI runs via `cron` or systemd timers

This is important because the app currently uses SQLite and FastAPI `BackgroundTasks`.
That makes single-process deployment the safest default.
Do not start multiple API workers unless you are also prepared to change the storage and job-execution model.

### 1. Create the runtime environment

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

### 2. Configure secrets and paths

For production, prefer real environment variables or a protected env file outside the repo.

Example env file:

```dotenv
WL_DATA_DIR=/srv/web-listening/data
WL_DB_PATH=/srv/web-listening/data/web_listening.db
WL_DOWNLOADS_DIR=/srv/web-listening/data/downloads

WL_OPENAI_API_KEY=your_real_api_key
WL_OPENAI_MODEL=gpt-4o-mini
WL_OPENAI_BASE_URL=https://api.openai.com/v1

WL_USER_AGENT=web-listening-bot/1.0
WL_REQUEST_TIMEOUT=30
```

Recommended file location:

```bash
/etc/web-listening/web-listening.env
```

Recommended permissions:

```bash
sudo chown root:root /etc/web-listening/web-listening.env
sudo chmod 600 /etc/web-listening/web-listening.env
```

### 3. Run the API with `uvicorn`

For production, bind the app to localhost and let Nginx handle public traffic:

```bash
.venv/bin/uvicorn web_listening.api.app:app --host 127.0.0.1 --port 8000
```

Use a single worker unless you replace SQLite and in-process background tasks with more production-oriented components.

### 4. Create a systemd service

Example `/etc/systemd/system/web-listening.service`:

```ini
[Unit]
Description=web-listening API
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/srv/web-listening/app
EnvironmentFile=/etc/web-listening/web-listening.env
ExecStart=/srv/web-listening/app/.venv/bin/uvicorn web_listening.api.app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now web-listening.service
sudo systemctl status web-listening.service
```

### 5. Put Nginx in front

Example `/etc/nginx/sites-available/web-listening`:

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then enable the site and reload Nginx:

```bash
sudo ln -s /etc/nginx/sites-available/web-listening /etc/nginx/sites-enabled/web-listening
sudo nginx -t
sudo systemctl reload nginx
```

If the service is public-facing, add HTTPS with Let's Encrypt.

### 6. Schedule checks and analysis

The API exposes endpoints to queue checks and downloads, but regular monitoring still needs a scheduler around the app.
The simplest production option is `cron`.

Example cron jobs:

```cron
*/30 * * * * cd /srv/web-listening/app && /srv/web-listening/app/.venv/bin/python -m web_listening.cli check >> /var/log/web-listening-check.log 2>&1
15 0 * * * cd /srv/web-listening/app && /srv/web-listening/app/.venv/bin/python -m web_listening.cli analyze >> /var/log/web-listening-analyze.log 2>&1
```

If you want automatic document downloads too, add a separate scheduled `download-docs` command or implement a dedicated scheduler flow in the app.

### 7. Operational notes

- Keep `WL_DATA_DIR` on persistent disk. SQLite and downloaded files live there.
- Back up the SQLite database and downloads directory together.
- If `WL_OPENAI_API_KEY` is empty, analysis still works, but it falls back to local summarization.
- First `check` creates the baseline snapshot only; it does not download files automatically.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Architecture

```
web_listening/
├── pyproject.toml
├── web_listening/
│   ├── config.py          # Pydantic settings
│   ├── models.py          # Data models
│   ├── cli.py             # Typer CLI
│   ├── blocks/
│   │   ├── crawler.py     # HTTP crawling + normalized snapshot creation
│   │   ├── normalizer.py  # HTML → cleaned HTML / Markdown / fit-Markdown
│   │   ├── diff.py        # Hashing, diffing, link extraction
│   │   ├── document.py    # Document download (no conversion; content_md left for doc_to_md module)
│   │   ├── storage.py     # SQLite persistence
│   │   └── analyzer.py    # AI/local change summarisation
│   └── api/
│       ├── app.py         # FastAPI application factory
│       └── routes.py      # API route handlers
└── tests/
```
