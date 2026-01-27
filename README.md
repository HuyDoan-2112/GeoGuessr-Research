# GeoGuessr Research

Research toolkit for driving Google Street View with a Playwright (Node) host and a Python API/server. Includes a wrapper CLI for quick manual testing and image capture.

## Quickstart (Docker, short)
1) Set your API key (do not commit it):
   - PowerShell: `setx GOOGLE_MAPS_API_KEY "YOUR_KEY"` then open a new terminal
2) Build + run:
   - `docker compose up --build`
3) Stop:
   - `docker compose down`

## Wrapper CLI (in Docker)
Run an interactive CLI against the running server:
```
docker compose exec geoguessr-worker python -m apps.wrapper_cli --base-url http://localhost:8000
```

Common commands:
```
init 37.7749 -122.4194
move north
scroll right 30
zoom in 1
state
end
```

## Outputs
- Images are saved in `/data/images` inside the container.
- Compose mounts that path to the `geoguessr-images` volume by default.

## Docs
- `docs/Run-Instructions-CLI.md`
- `docs/Run-Instructions-Compose.md`

## Notes
- Google Maps JS and Street View Static APIs must be enabled with billing.
- For Apple Silicon, you may need an arm64 Playwright image or emulation.
