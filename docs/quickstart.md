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
- Creates a dedicated `wheatley` Linux user configured with `/usr/local/bin/wheatley-shell` (disabling interactive logins and blocking host command chaining / `pct` & `qm destroy`) and scoped `sudoers` rules for host diagnostics and guest execution.
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
*(Note: `create-lxc.sh` automatically seeds `/etc/wheatley/secrets.env`, `config.yaml`, and `checks.yaml` from `/opt/wheatley/`. You do not need to run `cp`; directly edit the pre-created files.)*

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
   # Note: If checks.yaml already exists, pass --force / -f to overwrite (a .bak backup will be saved automatically).
   # You can also generate to a separate file for review: wheatley discover --output /etc/wheatley/checks.discovered.yaml
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

---

## 5. Proxmox Notification Webhooks (PVE 8.1+ & 9.x)

Wheatley includes a native webhook receiver listening on port `8088` (`/webhook/pve`) to intercept Proxmox VE notification events (such as vzdump backup failures, SMART alerts, and replication issues) and push alerts to Telegram.

### Webhook Target Setup in Proxmox Web GUI

1. Navigate to **Datacenter → Notifications → Notification Targets → Add → Webhook**:
   - **Name:** `wheatley-webhook`
   - **Method:** `POST`
   - **URL:** `http://<wheatley-ct-ip>:8088/webhook/pve`
   - **Headers:**
     - `Content-Type: application/json`
     - `Authorization: Bearer {{ secrets.token }}`
   - **Secrets:**
     - Key: `token` | Value: `<value of WEBHOOK_SECRET_TOKEN from /etc/wheatley/secrets.env>`
   - **Body Template:**
     ```json
     {
       "title": "{{ escape title }}",
       "message": "{{ escape message }}",
       "severity": "{{ severity }}",
       "data": {{ json fields }}
     }
     ```

2. Navigate to **Datacenter → Notifications → Notification Matchers → Add**:
   - **Name:** `wheatley-alerts`
   - **Target to notify:** Select `wheatley-webhook`
   - **Match Severity:** Check `warning` and `error`

> **Note on Webhook Authentication & Testing:**
> - **Required Secret Token:** Incoming requests to `/webhook/*` are rejected with `401 Unauthorized` if `WEBHOOK_SECRET_TOKEN` is unset or left at the default placeholder. `install/create-lxc.sh` automatically generates a random token in `/etc/wheatley/secrets.env`; if you are upgrading an existing installation, ensure `WEBHOOK_SECRET_TOKEN` is set in `/etc/wheatley/secrets.env` and restart `wheatley`.
> - **Test Button Behavior:** When clicking the **Test** button in Proxmox VE, PVE sends an event with severity `info`. Wheatley logs `info` events directly to the local audit log (`wheatley audit`) rather than spamming Telegram. Actual job failures or warnings (`warning` or `error`) immediately trigger formatted Telegram alerts.
