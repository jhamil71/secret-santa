# Free hosting on Oracle Cloud, with automatic backups

Oracle's "Always Free" servers cost nothing. You only pay for the domain name.
Keep in mind that Oracle can reclaim a free server that sits mostly idle, so the nightly backup below matters.

## 1. Buy the domain (a few minutes)
Any registrar works (Cloudflare, Namecheap, Porkbun...). Leave DNS for later.

## 2. Create the free Oracle account
Go to cloud.oracle.com and sign up. It asks for a card to verify you; you are not charged for Always Free resources.
Pick a home region close to you. This choice can't be changed later.

## 3. Create the server
Menu, Compute, Instances, **Create instance**.
- **Image:** Canonical Ubuntu 24.04.
- **Shape:** *Change shape*, then either *Ampere* (VM.Standard.A1.Flex, 1 OCPU and 6 GB, "Always Free-eligible"; best, but sometimes "out of capacity", in which case try again later or another availability domain) or *Specialty and previous generation*, VM.Standard.E2.1.Micro (always available, only 1 GB).
- **Networking:** keep the defaults and make sure *Assign a public IPv4 address* is on.
- **SSH keys:** choose *Generate a key pair* and download both (keep them; you only need them if you ever want a terminal).
- **Show advanced options, Management, "Paste cloud-init script":** paste the contents of `deploy/oracle-cloud-init.sh` from this repo after changing the `DOMAIN` and `ADMIN_PASSWORD` lines at the top.
  (The password is stored in your private Oracle account's server settings so the server can set itself up.)

Press **Create**. Setup takes about 5 to 10 minutes after the server shows *Running*.

## 4. Let web traffic in
Click the instance, then its **Subnet**, then the **Default Security List**, then **Add Ingress Rules** (twice):
- Source CIDR `0.0.0.0/0`, protocol TCP, destination port `80`
- Source CIDR `0.0.0.0/0`, protocol TCP, destination port `443`

## 5. Point the domain at the server
Copy the server's **Public IP address** from the instance page. In your registrar's DNS, add an **A record**:
name `santa` (making `santa.yourdomain.com`), value = that IP address. Use the same name you put in `DOMAIN`.
After a few minutes `https://santa.yourdomain.com` should load with a padlock, and `/admin` is the organizer page.

## 6. Automatic backups to Google Drive or Dropbox
The server already makes a dated backup file every night at 3:30 am in `/opt/secret-santa/backups`.
To also copy it to Google Drive or Dropbox you connect it once with a free tool called rclone.
This step needs a terminal and a computer with a web browser (Oracle's **Cloud Shell**, the `>_` icon at the top of the Oracle page, gives you a terminal).

1. In a terminal on your computer (or a GitHub Codespace), install rclone ([rclone.org/install](https://rclone.org/install/)) and run `rclone authorize "drive"` (or `"dropbox"`). It opens a browser to sign in and then prints a block of text starting `{"access_token"...`. Copy it.
2. In the server's terminal (from Cloud Shell: `ssh -i <your-key-file> ubuntu@SERVER_IP`), run `sudo apt-get install -y rclone`, then `sudo rclone config`:
   - `n` (new remote), name it `backup`, pick **drive** or **dropbox**, accept the defaults, answer **n** to "use web browser" (auto config), and paste the text from step 1 when asked.
3. Tell the backup where to upload, then test it:

       echo 'backup:santa-backups' | sudo tee /opt/secret-santa/.rclone-remote
       cd /opt/secret-santa && sudo RCLONE_REMOTE=backup:santa-backups ./scripts/backup.sh

   A `santa-backups` folder appears in your Drive or Dropbox. After that it happens nightly by itself, and files older than 60 days are removed.

## 7. If Oracle ever reclaims or loses the server
Create a new one the same way, download the newest backup from Drive or Dropbox, then on the new server:

    cd /opt/secret-santa && sudo docker compose -f docker-compose.prod.yml down
    tar -xzf santa-YYYY-MM-DD_HHMM.tar.gz        # makes a data/ folder; copy it into /opt/secret-santa/
    sudo docker compose -f docker-compose.prod.yml up -d

(Update the DNS A record to the new server's IP address.)

## Updating the site later
    cd /opt/secret-santa && sudo git pull && sudo docker compose -f docker-compose.prod.yml up -d --build
