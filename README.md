# ⚡ MLB Analytics Hub v2

Zero-dependency single-file Flask app — no templates folder needed.
All HTML is embedded directly in app.py.

## Deploy to Render
1. Push `app.py`, `requirements.txt`, `render.yaml`, `.gitignore` to GitHub root
2. Render auto-detects `render.yaml` → Deploy

## Run Locally
```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```


## Included fixes
- Added Open-Meteo stadium weather fallback for temperature, wind, and rain chance.
- Included matchup selectors and projection cards on the deep dive page.
