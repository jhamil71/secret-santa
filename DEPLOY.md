# Putting the site on the internet

You need two things: a small always-on computer (a "server") and a domain name.

## 1. Rent a small server
Any basic Linux server with 1 GB of memory is plenty for a family. A budget plan from DigitalOcean,
Hetzner or Linode is a few dollars a month. Choose **Ubuntu 24.04**, and note its **IP address**.

## 2. Point your domain at it
In your domain registrar's DNS settings, add an **A record**: name `santa` (or `@` for the bare domain),
value = the server's IP address. This gives you `santa.yourdomain.com`. It can take a few minutes to start working.

## 3. Install Docker on the server
Connect (your host's website usually has a "console" button) and run:

    curl -fsSL https://get.docker.com | sh

## 4. Get the code and set it up

    git clone https://github.com/jhamil71/secret-santa.git
    cd secret-santa
    printf 'DOMAIN=santa.yourdomain.com\nADMIN_PASSWORD=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(15))')" > .env
    cat .env          # write the password down somewhere safe
    docker compose -f docker-compose.prod.yml up -d --build

Open `https://santa.yourdomain.com`. The padlock certificate is obtained automatically the first time
(ports 80 and 443 must be open in your host's firewall settings, if it has one).

## 5. Look after it
- **Update** after new changes are pushed: `git pull && docker compose -f docker-compose.prod.yml up -d --build`
- **Back up** the `data/` folder (it holds the database and pictures). Copying it to your computer once a week is enough:
  `scp -r root@SERVER_IP:secret-santa/data ./santa-backup`
- **Change the organizer password:** edit `.env`, then `docker compose -f docker-compose.prod.yml up -d`
- **Logs:** `docker compose -f docker-compose.prod.yml logs -f santa`

Your organizer page is at `/admin`. Test the whole flow with a couple of family members before announcing it.
