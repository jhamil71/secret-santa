# Family Secret Santa

A small self-hosted gift exchange (think Elfster, but yours).

- **Rules for the draw**: couples can't draw each other, no one draws the same person as last year, no one draws themselves, and (optionally) no direct swaps.
- **Wish lists**: paste a product link and the name, picture and price are filled in automatically (you can edit them, or add your own picture link when a store blocks previews).
- **Organizer controls**: add people, set rules, draw names, reset or start next year. The organizer can't see who has whom, so you stay surprised too.
- **Personal links**: no accounts. Each person gets a private link.

## Run it

```bash
# 1. Edit ADMIN_PASSWORD in docker-compose.yml
docker compose up -d --build
# 2. Open http://localhost:8000 and click "Organizer"
```

Without Docker:

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=choose-one python app.py      # http://localhost:8000
```

If you don't set `ADMIN_PASSWORD`, one is generated on first start and saved in `data/admin_password.txt`.

## How a season goes

1. Log in as organizer and add everyone (including yourself, ticking "This is me").
2. Add couples under **Who can't draw whom**, and enter last year's matches.
3. Send each person their link (Copy link, or Email if you entered an address).
4. When everyone is in, press **Draw names**. Everyone opens their link to see who they drew and that person's wish list.
5. Next season, press **Start next year**. This year's matches are archived and avoided in the next draw; wish lists are kept.

## Settings (environment variables)

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Organizer password |
| `BASE_URL` | Public address used in personal links, e.g. `https://santa.example.com` |
| `COOKIE_SECURE=1` | Set once you serve over HTTPS |
| `DATA_DIR` | Where the database lives (default `./data`) |

## Good to know

- **Back up the `data/` folder.** It holds the database.
- **Put it behind HTTPS** (a reverse proxy like Caddy, or a tunnel) if it's reachable from the internet.
- **Link previews** read each store's page. Some (Amazon in particular) block this; in that case the form tells you and you fill in name, price and picture yourself.
- The organizer runs the server, so a determined organizer could still read the database. The app simply keeps the matches off the dashboard.

## Tests

```bash
pip install pytest && python -m pytest tests -q
```
