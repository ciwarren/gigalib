# GigaLib

A self-hosted Python web app that aggregates your game libraries from **Steam**, **EA Desktop**, **Ubisoft Connect**, and **Xbox/Game Pass** into a single unified dashboard — with AI-powered recommendations, IGDB ratings, and HowLongToBeat completion times.

## Features

- **Multi-platform sync** — Automatically detects installed games from Steam, EA Desktop (including encrypted IS file decryption), Ubisoft Connect, and Xbox/Game Pass
- **Game enrichment** — Fetches critic ratings, genres, descriptions, cover art (IGDB), and completion times (HowLongToBeat)
- **AI Assistant** — Chat with Gemini about your backlog: get recommendations by mood, time available, or genre; ask library stats; compare games
- **Launch games** — Start any game directly from the dashboard via platform-native URLs (steam://, origin2://, uplay://)
- **Production-ready** — Runs as a waitress WSGI server or Windows service

## Quick Start

### Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Fast Python package manager
- **Windows** — Required for local game detection (registry, file system scanning)

### 1. Clone and install

```powershell
git clone <your-repo-url> Gigalib
cd Gigalib
uv sync
```

### 2. Configure environment

Copy the example env file and fill in your API keys:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your keys:

| Variable | Where to get it |
|----------|----------------|
| `SECRET_KEY` | Any random string (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `STEAM_API_KEY` | [Steam Web API](https://steamcommunity.com/dev/apikey) |
| `STEAM_USER_ID` | Your Steam64 ID (find at [steamid.io](https://steamid.io)) |
| `XBOX_API_KEY` | [OpenXBL](https://xbl.io) — free tier works |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) |
| `TWITCH_CLIENT_ID` | [Twitch Dev Console](https://dev.twitch.tv/console) (for IGDB) |
| `TWITCH_CLIENT_SECRET` | Same Twitch app as above |

### 3. Configure platform paths

Edit `platforms.yaml` to match your install locations:

```yaml
steam:
  paths:
    - "C:\\Program Files (x86)\\Steam"
    - "D:\\SteamLibrary"  # Additional Steam libraries

ea:
  install_data:
    - "C:\\ProgramData\\EA Desktop\\InstallData"
  games_dirs:
    - "C:\\Program Files\\EA Games"

ubisoft:
  config_cache:
    - "C:\\Program Files (x86)\\Ubisoft\\Ubisoft Game Launcher\\cache\\configuration\\configurations"
  games_dirs:
    - "C:\\Program Files (x86)\\Ubisoft\\Ubisoft Game Launcher\\games"
```

### 4. Run

**Development:**

```powershell
uv run python scripts/run.py
```

Open http://127.0.0.1:5000

**Production:**

```powershell
uv run python scripts/serve.py --host 0.0.0.0 --port 8080
```

## Usage

1. **Sync** — Click the sync button to scan all platforms and detect your games
2. **Enrich** — Click enrich to fetch ratings, genres, and completion times from IGDB/HLTB
3. **Browse** — Filter by platform, genre, rating, or installed status
4. **Ask the AI** — Use the assistant chat to get personalized recommendations

## Project Structure

```
Gigalib/
├── platforms.yaml      # Platform path configuration
├── pyproject.toml      # Dependencies and project metadata
├── .env                # API keys (not committed)
├── gigalib/            # The package
│   ├── __init__.py     # Package init, create_app factory
│   ├── app.py          # Flask app configuration
│   ├── routes.py       # All HTTP routes
│   ├── models.py       # SQLAlchemy Game model
│   ├── platforms.py    # Platform detection (Steam/EA/Ubisoft/Xbox)
│   ├── enricher.py     # IGDB + HowLongToBeat enrichment
│   ├── assistant.py    # Gemini AI chat integration
│   └── templates/      # Jinja2 HTML templates
├── scripts/
│   ├── run.py          # Dev server (Flask debug mode)
│   ├── serve.py        # Production server (waitress)
│   ├── enrich.py       # CLI enrichment tool
│   ├── setup.ps1       # Interactive setup wizard
│   └── install_service.py  # Windows Task Scheduler service installer
└── instance/
    └── gigalib.db      # SQLite database (auto-created)
```

## API Keys Guide

### Steam
1. Go to https://steamcommunity.com/dev/apikey
2. Register a domain (can be `localhost`)
3. Copy the key

### Xbox (OpenXBL)
1. Create free account at https://xbl.io
2. Go to Settings → API Keys
3. Copy your key

### IGDB (via Twitch)
1. Create a Twitch account and go to https://dev.twitch.tv/console
2. Register a new application (any name, category: Website Integration, OAuth redirect: `http://localhost`)
3. Copy the Client ID and generate a Client Secret

### Gemini (for AI Assistant)
1. Go to https://aistudio.google.com/apikey
2. Create an API key
3. Free tier has rate limits (~15 requests/minute)

## Troubleshooting

- **"No games found after sync"** — Check `platforms.yaml` paths match your actual install directories
- **"Enrichment returning 0"** — Verify your Twitch Client ID/Secret are correct (used for IGDB)
- **"Assistant says rate limited"** — Gemini free tier caps at ~15 req/min; wait a moment and retry
- **EA games missing** — EA Desktop must be installed; the app decrypts the IS file for the full library
