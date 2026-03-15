# web_listening

Monitor websites for changes, download documents, and generate AI summaries.

## Features

- **Website monitoring** – crawl sites, compute content diffs, detect new links and documents
- **Document downloading** – fetch PDFs, DOCX, XLSX and save locally; content conversion is handled by the separate `doc_to_md` module
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
| `DELETE` | `/api/v1/sites/{id}` | Deactivate a site |
| `POST` | `/api/v1/sites/{id}/check` | Queue a check (background) |
| `POST` | `/api/v1/sites/{id}/download-docs` | Queue document download |
| `GET` | `/api/v1/changes` | List changes (filter by `site_id`, `since`) |
| `GET` | `/api/v1/documents` | List documents (filter by `institution`, `site_id`) |
| `POST` | `/api/v1/analyze` | Run analysis and store report |
| `GET` | `/api/v1/analyses` | List analysis reports |

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
│   │   ├── crawler.py     # HTTP crawling + text extraction
│   │   ├── diff.py        # Hashing, diffing, link extraction
│   │   ├── document.py    # Document download (no conversion; content_md left for doc_to_md module)
│   │   ├── storage.py     # SQLite persistence
│   │   └── analyzer.py    # AI/local change summarisation
│   └── api/
│       ├── app.py         # FastAPI application factory
│       └── routes.py      # API route handlers
└── tests/
```
