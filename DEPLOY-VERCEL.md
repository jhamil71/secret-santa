# Free hosting on Vercel (with a free Turso database)

Vercel runs the website. It has no permanent disk, so the data (people, wish lists, even the pictures) lives in
**Turso**, a free online database. You only pay for the domain name. Everything below is done on websites,
so it works from a phone.

You will make two free accounts (both can sign in with your GitHub login), then paste four settings into Vercel.

## 1. Create the database (Turso)
1. Go to **turso.tech**, choose **Sign up**, and continue with GitHub.
2. Create a database. Name it `santa` and pick the region closest to you.
3. Open the database and find its **URL** (starts with `libsql://`) and a button to **create a token**
   (sometimes under *Connect* or *Tokens*). Create one with full access and copy both somewhere you can paste from.
   Treat the token like a password.

## 2. Make a secret key
Any long random text. In a password manager, generate a 40-character password, or in a Codespace run
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## 3. Put the site on Vercel
1. Go to **vercel.com**, sign up with GitHub, and allow it to see your `secret-santa` repository.
2. **Add New**, then **Project**, then import `jhamil71/secret-santa`.
3. If it asks for a framework, choose **Flask** (or leave it on automatic). Leave the other build settings alone.
4. Open **Environment Variables** and add these five (name on the left, value on the right):

   | Name | Value |
   |---|---|
   | `ADMIN_PASSWORD` | your organizer password (at least 8 characters, 20 is better) |
   | `SECRET_KEY` | the long random text from step 2 |
   | `TURSO_DATABASE_URL` | the `libsql://...` URL from step 1 |
   | `TURSO_AUTH_TOKEN` | the token from step 1 |
   | `BASE_URL` | `https://santa.yourdomain.com` (use the address people will really visit; you can add it after step 4) |

5. Press **Deploy**. In a minute you get an address like `secret-santa-xxxx.vercel.app`. Open it, then open `/admin`
   and log in with your organizer password to confirm everything works.

## 4. Use your own domain
In the Vercel project, open **Settings**, then **Domains**, and add `santa.yourdomain.com`.
Vercel then shows the DNS record to add (usually a CNAME or A record). Add exactly what it shows at your domain registrar.
A few minutes later the site is live at your domain with a padlock (Vercel provides the certificate for free).
Then make sure `BASE_URL` matches, and in **Deployments** press **Redeploy** after any settings change.

## 5. Backups
Your data lives only in Turso, so keep a copy. From a computer or a GitHub Codespace:

    TURSO_DATABASE_URL='libsql://...' TURSO_AUTH_TOKEN='...' python3 scripts/backup_turso.py

That saves `backups/santa-<date>.db` containing everything, pictures included. Do it before the draw and again every
few weeks. (You can store it in Google Drive or Dropbox by hand. To restore, create a new Turso database and ask me for a
restore script.)

## Good to know
- **Updating the site:** every time new code is pushed to GitHub, Vercel republishes it automatically.
- **Slow first visit:** after a quiet spell the first page can take a second or two to wake up. After that it is quick.
- **Link previews:** Vercel gives a function about 10 seconds on small plans, so a very slow store may not preview in time. The gift is still saved and its picture is retried later.
- **Troubleshooting:** in Vercel, open the project's **Logs** tab and look for red lines. A message beginning *Set a SECRET_KEY...* or *Set an ADMIN_PASSWORD...* means that setting is missing from step 3.
- **Vercel's free plan** is for non-commercial use, which a family gift exchange is.
