# browser-ai-engine

Shared browser automation engine for Gemini and DeepSeek web UIs.

Used as a shared dependency by:
- [GemiPersonaPro_DT](https://github.com/liewcc/GemiPersonaPro_DT)
- [Gemi_MCP](https://github.com/liewcc/Gemi_MCP)

## Structure

```
core/
  browser_engine.py     # Generic browser lifecycle (Playwright)
  engine_service.py     # FastAPI REST wrapper (port 18800)
  config_utils.py       # Config read/write
  api_client.py         # HTTP client
  health_parser.py      # Health status parsing
  providers/
    base.py             # Abstract ProviderAdapter interface
    gemini.py           # Gemini web UI selectors and flows
    deepseek.py         # DeepSeek (future)
```

## Peer dependency

Each consuming project must provide its own `processing_utils.py`
in the same `core/` directory (image save/processing, project-specific).

## Switching providers

Pass `provider` at engine start. Default is `gemini`.
Switching requires an engine restart (stop → start with new provider).

## Updating

```bash
git pull origin main
```
