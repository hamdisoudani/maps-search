# Google Maps Search via Daytona Sandbox 🗺️

Extract **structured business data** from Google Maps search results using a headless Chromium browser running inside a Daytona sandbox — no API key required, clean exit IP, CAPTCHA-resistant.

## Features

- **Full business data extraction** — name, rating, category, address, phone, website, hours, coordinates, business status, wheelchair accessibility, and more
- **Persistent sandbox mode** — reuses the same Daytona sandbox across queries (6-8s per search instead of 60s cold start)
- **Clean exit IP** — all traffic routes through Daytona's infrastructure, bypassing VPS IP blocks and denylists
- **CAPTCHA detection** — automatically detects Google rate-limiting pages and reports them
- **Multiple output formats** — pretty-print terminal, JSON, or CSV
- **No API key from Google** — works entirely through browser automation, no Google Places API billing required

## Quick Start

### Prerequisites

- Python 3.10+
- A [Daytona](https://daytona.io) account with API key
- `daytona-sdk` Python package

### Installation

```bash
# Clone the repo
git clone https://github.com/hamdisoudani/maps-search.git
cd maps-search

# Install dependencies
pip install daytona-sdk

# Set up your Daytona API key
echo 'DAYTONA_API_KEY=dtn_your_key_here' >> ~/.hermes/.env

# Make the script executable
chmod +x maps_search.py

# Optional: add to PATH
alias maps-search="python3 $(pwd)/maps_search.py"
```

### Usage

```bash
# Basic search
python3 maps_search.py "Plumber in Paris"

# Structured JSON output
python3 maps_search.py "Sushi Tokyo" --limit 5 --output json

# CSV for spreadsheets
python3 maps_search.py "Cafe near me" -o csv

# One-shot mode (no caching)
python3 maps_search.py --oneshot "Quick search"

# Manage persistent sandbox
python3 maps_search.py --status
python3 maps_search.py --destroy
```

## Extracted Data Fields

| Field | Description | Example |
|---|---|---|
| `name` | Business name | "Arti-Pro Plombier" |
| `rating` | Star rating (float) | 4.6 |
| `category` | Business type | "Plumber" |
| `address` | Street address | "1 Rue de l'Assomption" |
| `hours` | Operating hours | "Open 24 hours" |
| `phone` | Phone number | "+33 1 40 50 26 26" |
| `website` | Business website | "https://artipro-plomberie.fr/" |
| `lat` / `lng` | GPS coordinates | 48.852 / 2.275 |
| `business_status` | Open / temporarily closed | "open" |
| `wheelchair_accessible` | Accessibility flag | true / false |
| `price_level` | Cost indicator ($-$$$$) | (category-dependent) |
| `plus_code` | Google Plus Code | (when available) |

## How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────────┐     ┌────────────┐
│  Your CLI   │ ──▶ │  maps_search.py  │ ──▶ │  Daytona Sandbox     │ ──▶ │  Google    │
│             │     │  (orchestrator)  │     │  (Ubuntu + Chromium) │     │  Maps      │
└─────────────┘     └──────────────────┘     └──────────────────────┘     └────────────┘
                           │                          │                        │
                      base64 payload              Playwright script        Search results
                      + sandbox mgmt              + DOM extraction         + JSON output
```

1. **maps_search.py** spawns (or reuses) a Daytona sandbox with a clean exit IP
2. Installs Playwright + Chromium inside the sandbox (first run only)
3. Renders a Playwright automation script with your search query
4. The script launches headless Chromium, navigates to Google Maps, and executes the search
5. Extracts business data from the DOM using JavaScript evaluation
6. Returns structured JSON to the orchestrator
7. Sandbox is either destroyed (oneshot) or kept alive for the next query (persistent)

## Speed

| Mode | Time | Notes |
|---|---|---|
| Cold start (oneshot) | ~6-8s | Full sandbox spawn + browser install |
| Warm (persistent) | ~6-8s | Reuses live sandbox, no setup overhead |

The persistent sandbox mode uses a local cache file (`~/.hermes/cache/maps_sandbox.json`) to track the running sandbox. Use `--destroy` to clean up when done.

## Prerequisites

- **Daytona API Key** — sign up at [daytona.io](https://daytona.io) and get your API key
- **Python 3.10+** with `daytona-sdk` installed
- The tool handles all dependencies inside the sandbox automatically

## Project Structure

```
maps-search/
├── maps_search.py                # Main CLI orchestrator
├── maps_search_payload.py.tpl    # Playwright script template (deployed to sandbox)
├── README.md                     # This file
└── .gitignore                    # Git ignore rules
```

## Security

- **No API keys in code** — the Daytona API key is loaded from `~/.hermes/.env` at runtime, never hardcoded
- **Sandbox isolation** — all browser automation runs inside an isolated Daytona sandbox with a clean IP
- **Payload safety** — user queries are sanitized (printable-only, max 200 chars) and base64-encoded before transmission
- **No Google API costs** — this tool uses browser automation, not paid Google APIs
- **CAPTCHA protection** — automatically detects rate-limiting pages and exits gracefully

## License

MIT

## Author

Built by [@mrdedatn](https://github.com/hamdisoudani)
