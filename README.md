# Family Secret Santa

A small self-hosted gift exchange (think Elfster, but yours).

- **Rules for the draw**: couples can't draw each other, no one draws the same person as last year, no one draws themselves, and (optionally) no direct swaps.
- **Wish lists**: paste a product link and the name, picture and price are found automatically, the way Google Sheets previews a link. Nobody uploads or pastes a picture. Pictures are copied to `data/img` so they keep working, and gifts saved without one are retried in the background. Everyone's list is also browsable by the whole family from their own page.
- **Organizer controls**: add people, set rules, draw names, reset or start next year. The page saves as you go (no Save buttons; the My wish list page works the same way, with every gift open for editing), shows a small "Saved ✓" confirmation, and updates in place without reloading. The organizer can't see who has whom, so you stay surprised too.
- **Shareable wish list page**: every person also gets a separate public link (`/list/...`) for their wish list alone, shown on the landing page with Copy link and Text it buttons. It has no Secret Santa wording and shows no one else's information, so they can send it to anyone (grandparents, friends, anyone outside the exchange). It's separate from the private link, so sharing it never reveals who anyone drew. It's hidden from search engines, but anyone with the link can view it.
- **Checking things off**: anyone with the link, family or not, can tap **I bought this** on a gift. Everyone else then sees an anonymous "Already bought", and only the person who checked it can undo it. The list's owner never sees the checkmarks. (The site recognises the owner by their browser, after they've opened their private link. If they open their own shared link on a device that has never opened their private link, they'd see the checkmarks, so keep that one for others.)
- **Phone-number sign-in**: no passwords or accounts. The organizer enters each person's name and mobile number; family type their number on the main page and see "Welcome, <name>". The landing page lists everyone, each name linking to that person's public wish list, and shows who you're buying for once names are drawn. Every row has a Copy link button, and your own row has Edit list instead of opening your public list, and opening your own public list is blocked so you can't spoil what's been bought for you.

## Run it

```bash
# 1. Put your organizer password in a file named .env (it is never committed to git):
echo 'ADMIN_PASSWORD=pick-something-long' > .env
docker compose up -d --build
# 2. Open http://localhost:8000 and click "Organizer"
```

Without Docker:

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=choose-one python app.py      # http://localhost:8000
```

If you don't set `ADMIN_PASSWORD`, one is generated on first start, printed in the terminal, and saved in `data/admin_password.txt`.

### On the internet

- Free, from your phone: [DEPLOY-VERCEL.md](DEPLOY-VERCEL.md) (Vercel plus a free Turso database).
- Free server: [DEPLOY-ORACLE.md](DEPLOY-ORACLE.md).
- Any paid server: [DEPLOY.md](DEPLOY.md) for a step-by-step guide (small server, domain name, automatic HTTPS).

### From your phone (GitHub Codespaces)

1. On github.com, open this repo, tap **Code → Codespaces → Create codespace on main**.
2. When the terminal appears, run `python app.py` and note the generated organizer password it prints.
3. Open the **Ports** tab, find port 8000, and open it in the browser. To try personal links on other devices, set the port's visibility to **Public** (only while testing).
4. Stop the codespace when done so it stops using your free allowance.

## How a season goes

1. Log in as organizer and add everyone, including yourself, with the mobile number each person will use to sign in.
2. Add couples under **Who can't draw whom**, and enter last year's matches.
3. Tell everyone to visit the site and enter their number. Sign-in works before the draw, so people can start their wish lists right away. Don't open other people's links yourself: a link signs you in as them and shows who they drew.
4. When everyone is in, press **Draw names**. Everyone signs in to see who they drew and that person's wish list.
5. Next season, press **Start next year**. This year's matches are archived and avoided in the next draw; wish lists are kept.

## Settings (environment variables)

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Organizer password (at least 8 characters; placeholders like "change-me" are refused). Organizer sign-in lasts 12 hours and is rate limited |
| `BASE_URL` | Public address used in personal links, e.g. `https://santa.example.com` |
| `COOKIE_SECURE=1` | Set once you serve over HTTPS |
| `TRUST_PROXY=1` | Set when behind a reverse proxy or port forwarder, so links use its address |
| `DATA_DIR` | Where the database lives (default `./data`) |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | Use a hosted Turso database instead of the local file (needed on Vercel; pictures are then stored in it too) |
| `SECRET_KEY` | Session signing key (required on Vercel; generated into `data/` elsewhere) |

## Good to know

- **The welcome message** (with the spending limit) on the family landing page is plain text in `templates/home.html`. Edit it there to change the wording or amount.
- **Phone-number sign-in is convenience, not strong security.** Anyone who knows a family member's number could sign in as them and see who they drew. That is fine for a family exchange; attempts are rate limited, and the site isn't listed anywhere. Real text-message codes would need a paid texting service, so ask if you want that later.

- **Back up the `data/` folder.** It holds the database.
- **Put it behind HTTPS** (a reverse proxy like Caddy, or a tunnel) if it's reachable from the internet.
- **Link previews** read each store's page. Stores often refuse servers in the cloud (Vercel, for one) outright. When that happens the site asks outside services (Microlink and Jina Reader for the page, wsrv.nl for the picture), which fetch from their own machines, so those companies see the gift and picture links. Set `LINK_PREVIEW_FALLBACKS=0` to turn that off, or `MICROLINK_API_KEY` if you have a paid key (the free allowance is small). For the most stubborn stores you can add a ScrapingBee account: set `SCRAPINGBEE_API_KEY` and it is used as a last resort (it is pay-per-use after a free trial). The organizer page *Check gift pictures* shows what each store answered. If nothing works, the gift shows a tile with the store's name instead of a picture, and the price may need typing in. Amazon links get their picture from the product code.
- **Fonts** (Besley and Public Sans) are bundled in `static/fonts` under the SIL Open Font License, so the site makes no outside requests.
- The organizer runs the server, so a determined organizer could still read the database. The app simply keeps the matches off the dashboard.

## Tests

```bash
pip install pytest && python -m pytest tests -q
TEST_TURSO=1 python -m pytest tests/test_all.py -q    # same tests against a fake hosted database
# optional browser tests (organizer page, and the family's sign-in journey):
pip install playwright && playwright install chromium
python tests/e2e_browser.py && python tests/e2e_member.py
```
