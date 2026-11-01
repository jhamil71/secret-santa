# Family Secret Santa

A small self-hosted gift exchange (think Elfster, but yours).

- **Rules for the draw**: couples can't draw each other, no one draws the same person as last year, no one draws themselves, and (optionally) no direct swaps.
- **Wish lists**: paste a product link and the name, picture and price are filled in automatically (you can edit them, or add your own picture link when a store blocks previews). Everyone's list is also browsable by the whole family from their own page.
- **Organizer controls**: add people, set rules, draw names, reset or start next year. The page saves as you go (no Save buttons; the My wish list page works the same way, with every gift open for editing), shows a small "Saved ✓" confirmation, and updates in place without reloading. The organizer can't see who has whom, so you stay surprised too.
- **Shareable wish list page**: every person also gets a separate public link (`/list/...`) for their wish list alone, shown on the landing page with Copy link and Text it buttons. It has no Secret Santa wording and shows no one else's information, so they can send it to anyone (grandparents, friends, anyone outside the exchange). It's separate from the private link, so sharing it never reveals who anyone drew. It's hidden from search engines, but anyone with the link can view it.
- **Checking things off**: anyone with the link, family or not, can tap **I bought this** on a gift. Everyone else then sees an anonymous "Already bought", and only the person who checked it can undo it. The list's owner never sees the checkmarks. (The site recognises the owner by their browser, after they've opened their private link. If they open their own shared link on a device that has never opened their private link, they'd see the checkmarks, so keep that one for others.)
- **Phone-number sign-in**: no passwords or accounts. The organizer enters each person's name and mobile number; family type their number on the main page and see "Welcome, <name>". The landing page lists everyone, each name linking to that person's public wish list, and shows who you're buying for once names are drawn. Your own entry shows your shareable link (Copy link / Text it) and an Edit my list button instead, and opening your own public list is blocked so you can't spoil what's been bought for you.
- **Personal links** (optional): tap **Text link** next to a name and your own messages app opens with a sign-in link ready to send (nothing is sent automatically, and no texting service is needed). Opening the link signs that person in without typing their number.

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

If you don't set `ADMIN_PASSWORD`, one is generated on first start, printed in the terminal, and saved in `data/admin_password.txt`.

### From your phone (GitHub Codespaces)

1. On github.com, open this repo, tap **Code → Codespaces → Create codespace on main**.
2. When the terminal appears, run `python app.py` and note the generated organizer password it prints.
3. Open the **Ports** tab, find port 8000, and open it in the browser. To try personal links on other devices, set the port's visibility to **Public** (only while testing).
4. Stop the codespace when done so it stops using your free allowance.

## How a season goes

1. Log in as organizer and add everyone, including yourself, with the mobile number each person will use to sign in.
2. Add couples under **Who can't draw whom**, and enter last year's matches.
3. Tell everyone to visit the site and enter their number (or tap **Text link** next to each person to send a sign-in link). Sign-in works before the draw, so people can start their wish lists right away. Don't open other people's links yourself: a link signs you in as them and shows who they drew.
4. When everyone is in, press **Draw names**. Everyone signs in to see who they drew and that person's wish list.
5. Next season, press **Start next year**. This year's matches are archived and avoided in the next draw; wish lists are kept.

## Settings (environment variables)

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Organizer password |
| `BASE_URL` | Public address used in personal links, e.g. `https://santa.example.com` |
| `COOKIE_SECURE=1` | Set once you serve over HTTPS |
| `TRUST_PROXY=1` | Set when behind a reverse proxy or port forwarder, so links use its address |
| `DATA_DIR` | Where the database lives (default `./data`) |

## Good to know

- **Phone-number sign-in is convenience, not strong security.** Anyone who knows a family member's number could sign in as them and see who they drew. That is fine for a family exchange; attempts are rate limited, and the site isn't listed anywhere. Real text-message codes would need a paid texting service, so ask if you want that later.

- **Back up the `data/` folder.** It holds the database.
- **Put it behind HTTPS** (a reverse proxy like Caddy, or a tunnel) if it's reachable from the internet.
- **Link previews** read each store's page. Some (Amazon in particular) block this; in that case the form tells you and you fill in name, price and picture yourself.
- The organizer runs the server, so a determined organizer could still read the database. The app simply keeps the matches off the dashboard.

## Tests

```bash
pip install pytest && python -m pytest tests -q
# optional browser tests (organizer page, and the family's sign-in journey):
pip install playwright && playwright install chromium
python tests/e2e_browser.py && python tests/e2e_member.py
```
