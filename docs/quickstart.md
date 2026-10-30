# Wheatley Quick Start Guide

This guide walks you through setting up Wheatley from scratch on your Proxmox VE homelab.

---

## 1. Prerequisites

1. **Proxmox VE 8.x or 9.x host** with root access.
2. **Telegram Bot Token**: Created via [@BotFather](https://t.me/botfather).
3. **Telegram Numeric User ID**: Obtained via [@userinfobot](https://t.me/userinfobot) (only messages from this ID will be accepted).
4. **LLM Provider Credentials**:
   - Google Gemini API key (recommended for fast cloud reasoning), OR
   - Local Ollama instance (100% private offline homelab inference), OR
   - OpenAI API key, Groq, or any OpenAI-compatible API endpoint.

---

## 2. Provisioning & Host Setup

Clone Wheatley onto your primary Proxmox VE host:

```bash
git clone https://github.com/loosliashton/wheatley.git /root/wheatley
cd /root/wheatley
chmod +x install/*.sh
```

### Step A: Configure the Primary Proxmox Host
Run the host setup script on your primary Proxmox VE node:

```bash
./install/host-setup.sh --node-index 1
```

This script:
- Creates a dedicated `wheatley@pve` user and `wheatley@pve!bot` API token with `PVEAuditor` read-only privileges on `/`.
- Creates a restricted `wheatley` Linux user locked to `/usr/local/bin/wheatley-shell` with narrow sudoers rules.
- Prints your `NODE1_TOKEN_MONITOR` and `NODE1_TLS_FINGERPRINT` entries to save for `/etc/wheatley/secrets.env` (created inside the container in Step B).

### Step B: Provision the Dedicated LXC Container
Create an unprivileged Debian container (default: `CT 110`) to run Wheatley:

```bash
./install/create-lxc.sh --ip 192.168.1.110/24 --gateway 192.168.1.1
```

*Customizable flags: `--ctid <id>`, `--hostname <name>`, `--storage <pool>`, `--bridge <bridge>`, `--memory <mb>`.*

`create-lxc.sh` automatically:
- Installs `/opt/wheatley`, builds `/opt/wheatley/venv`, and symlinks `/usr/bin/wheatley` and `/usr/local/bin/wheatley`.
- Seeds `/etc/wheatley/config.yaml`, `/etc/wheatley/checks.yaml`, and `/etc/wheatley/secrets.env`.
- Installs and enables `/etc/systemd/system/wheatley.service`.
- Generates `/var/lib/wheatley/.ssh/id_ed25519` inside the container, authorizes it on the local Proxmox host, and prints the `--ssh-key` command to run on any secondary cluster nodes.

*(Optional) If you have additional Proxmox cluster nodes, run `./install/host-setup.sh --node-index 2 --ssh-key "<pubkey>"` on each secondary node using the public key printed by `create-lxc.sh`.*

---

## 3. Configuration

Enter the container (`pct enter 110`):

1. **Secrets (`/etc/wheatley/secrets.env`):**
   ```bash
   nano /etc/wheatley/secrets.env
   ```
   Add your `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, LLM API key, and the `NODE1_TOKEN_MONITOR` / `NODE1_TLS_FINGERPRINT` values printed by `host-setup.sh`.

2. **Configuration (`/etc/wheatley/config.yaml`):**
   ```bash
   nano /etc/wheatley/config.yaml
   ```
   Update your node `name`, `api_url`, and `ssh.host` IP addresses.

3. **Checks Configuration (`/etc/wheatley/checks.yaml`):**
   Checks are stored in `/etc/wheatley/checks.yaml` (enforced by the configuration loader). You can auto-generate checks for your running VMs, containers, and services, or edit the file manually:
   ```bash
   wheatley discover --output /etc/wheatley/checks.yaml
   nano /etc/wheatley/checks.yaml
   ```

4. **Validate:**
   ```bash
   wheatley config validate
   ```

---

## 4. Starting the Service

Inside `CT 110`, start the systemd daemon:

```bash
systemctl start wheatley
systemctl status wheatley
```

Send `/status` to your bot in Telegram to verify connectivity!
