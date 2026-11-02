#!/bin/bash
# Paste this whole script into Oracle's "Cloud-init script" box when creating the server
# (Create instance > Show advanced options > Management > Paste cloud-init script).
# Change the two values below first. It installs everything, starts the site, and schedules a nightly backup.

DOMAIN="santa.yourdomain.com"
ADMIN_PASSWORD="put-a-20-character-password-here"

# ---------------------------------------------------------------------------------------------
set -x
exec > /var/log/santa-setup.log 2>&1

# Oracle's Ubuntu images block web traffic in their own firewall; open ports 80 and 443.
iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
apt-get update -y && apt-get install -y iptables-persistent git curl
netfilter-persistent save

# A little swap so building on a small server doesn't run out of memory.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

curl -fsSL https://get.docker.com | sh

cd /opt
git clone https://github.com/jhamil71/secret-santa.git
cd secret-santa
printf 'DOMAIN=%s\nADMIN_PASSWORD=%s\n' "$DOMAIN" "$ADMIN_PASSWORD" > .env
chmod 600 .env
docker compose -f docker-compose.prod.yml up -d --build

# Nightly backup at 3:30 am (uploads too once you set up rclone, see DEPLOY-ORACLE.md)
cat > /etc/cron.d/santa-backup <<CRON
30 3 * * * root cd /opt/secret-santa && RCLONE_REMOTE=\$(cat /opt/secret-santa/.rclone-remote 2>/dev/null) ./scripts/backup.sh >> /var/log/santa-backup.log 2>&1
CRON
