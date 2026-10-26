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

## 2. Host Setup

Clone Wheatley onto your primary Proxmox VE host:

```bash
git clone https://github.com/loosliashton/wheatley.git /root/wheatley
cd /root/wheatley
```

Run the host setup script on **each** Proxmox VE node you want Wheatley to monitor:

```bash
chmod +x install/*.sh
./install/host-setup.sh
```

This script:
- Creates a dedicated `wheatley@pve` system user.
- Generates an API token `wheatley@pve!bot` with `PVEAuditor` read-only privileges on `/`.
- Generates a local SSH key pair and configures the `wheatley-shell` restricted command wrapper.
- Displays the API token secret and TLS fingerprint needed for your configuration.

---

## 3. Provisioning the Dedicated LXC Container

Create an unprivileged Debian container (CT 110) to run Wheatley:

```bash
./install/create-lxc.sh --ip 192.168.1.110/24 --gateway 192.168.1.1
```

*Customizable flags: `--ctid <id>`, `--hostname <name>`, `--storage <pool>`, `--bridge <bridge>`, `--memory <mb>`.*

---

## 4. Configuration

Inside CT 110 (or via `pct enter 110`):

1. **Secrets (`/etc/wheatley/secrets.env`):**
   ```bash
   cp /opt/wheatley/secrets.example.env /etc/wheatley/secrets.env
   chmod 600 /etc/wheatley/secrets.env
   chown wheatley:wheatley /etc/wheatley/secrets.env
   nano /etc/wheatley/secrets.env
   ```
   Add your `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, LLM keys, and node tokens.

2. **Configuration (`/etc/wheatley/config.yaml`):**
   ```bash
   cp /opt/wheatley/config.example.yaml /etc/wheatley/config.yaml
   nano /etc/wheatley/config.yaml
   ```

3. **Checks Configuration (`/etc/wheatley/checks.yaml`):**
   Checks must be defined in a separate `checks.yaml` file (enforced by the configuration loader). This isolates cluster nodes and API tokens from check thresholds, and allows Wheatley to conversationally propose and apply updates (e.g. ignoring dormant containers):
   ```bash
   cp /opt/wheatley/checks.example.yaml /etc/wheatley/checks.yaml
   nano /etc/wheatley/checks.yaml
   ```

4. **Validate:**
   ```bash
   wheatley config validate
   ```

---

## 5. Starting the Service

Enable and start the systemd daemon:

```bash
systemctl enable --now wheatley
systemctl status wheatley
```

Send `/status` to your bot in Telegram to verify connectivity!
