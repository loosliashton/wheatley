# 🤖 Wheatley

> **AI Homelab Administration & Monitoring Agent for Proxmox VE**  
> Proactive monitoring, Telegram alerts, and AI-assisted troubleshooting with an **enforced interactive approval gate** for all state changes.

---

## 🌟 Overview

Wheatley is an autonomous administration agent purpose-built for homelab operators running Proxmox VE. Unlike traditional monitoring dashboards or unconstrained AI tools, Wheatley is built around a **strict safety model**:

- 🛡️ **Interactive Approval Gate:** Safe, read-only diagnostic commands (e.g. `uptime`, `df -h`, `docker logs`) run immediately. **Mutating operations (restarts, stops, edits) strictly require human approval** via Telegram inline buttons (**[Approve]** / **[Deny]**).
- 🔒 **Least Privilege & Native Security:** Runs inside a dedicated, unprivileged Debian LXC container. Connects to Proxmox VE nodes using native `@pve` API tokens with `PVEAuditor` permissions.
- 🌐 **Shareable & Config-Driven:** Zero hardcoded IPs, hostnames, or credentials. Everything lives in `/etc/wheatley/config.yaml` and is backed by environment variable interpolation (`${VAR}`) from `/etc/wheatley/secrets.env`.
- 📊 **Multi-Node Cluster & Standalone Support:** Monitors single standalone nodes or distributed multi-node topologies.
- 🤖 **Bring Your Own LLM (LLM-Agnostic):** Plug in your choice of model: Google Gemini, local Ollama (100% offline private homelab models like `llama3.2`, `qwen2.5`, `mistral`), OpenAI (`gpt-4o-mini`), Groq, DeepSeek, vLLM, or any OpenAI-compatible endpoint. No artificial billing limits or execution pauses.

---

## 🏗️ Architecture

```mermaid
flowchart TD
    User([Telegram User]) <-->|"Long-polling (outbound)"| Bot["Telegram Interface (CT 110)"]
    
    subgraph LXC ["CT 110: Wheatley LXC Agent"]
        Bot <--> Daemon["Wheatley Daemon & Orchestrator"]
        Daemon <--> Audit[("SQLite Audit DB")]
        Daemon <--> Config["/etc/wheatley/config.yaml"]
        Daemon <--> Client["ProxmoxClient (proxmoxer)"]
    end

    subgraph PVE ["Proxmox Infrastructure"]
        Client -->|"REST API (TLS Pinned)"| Node1["Node 1 (e.g., Primary)"]
        Client -->|"REST API (TLS Pinned)"| Node2["Node 2 (e.g., Secondary)"]
        Node1 --> Guests1["VMs & LXCs (Plex, Frigate, Docker)"]
        Node2 --> Guests2["VMs & LXCs (Home Assistant)"]
    end
```

---

## 🚀 Quick Start & Installation

### Prerequisites
1. **Proxmox VE Host** (PVE 8.x or 9.x) with root access.
2. **Telegram Bot Token** from [@BotFather](https://t.me/botfather).
3. **Your Numeric Telegram User ID** from [@userinfobot](https://t.me/userinfobot).
4. **LLM Credentials**: Google AI Studio API Key, local Ollama instance, OpenAI API Key, Groq, or any OpenAI-compatible provider.

---

### Step 1: Clone Repository onto Proxmox Host
SSH into your primary Proxmox host and clone the repository:
```bash
git clone https://github.com/your-username/wheatley.git /root/wheatley
cd /root/wheatley
```

### Step 2: Provision the Dedicated LXC Container
Run the automated LXC installer to provision an unprivileged Debian container (defaults to CTID `110`, 1 vCPU, 1GB RAM, 8GB disk):
```bash
chmod +x install/*.sh
./install/create-lxc.sh --ip 192.168.1.110/24 --gateway 192.168.1.1
```
*Options: `--ctid <id>`, `--hostname <name>`, `--storage <pool>`, `--bridge <bridge>`, `--memory <mb>`.*

### Step 3: Configure Proxmox Nodes
Run the host setup script on **each** Proxmox node you want Wheatley to monitor:
```bash
./install/host-setup.sh
```
This script:
- Creates a native `wheatley@pve` user.
- Generates an API token (`wheatley@pve!bot`) with the `PVEAuditor` role on `/`.
- Prints the exact API token secret and SHA-256 TLS certificate fingerprint.

### Step 4: Populate Secrets
Inside CT 110 (or via `pct enter <ctid>`), create `/etc/wheatley/secrets.env`:
```bash
cp .env.example /etc/wheatley/secrets.env
chmod 600 /etc/wheatley/secrets.env
chown wheatley:wheatley /etc/wheatley/secrets.env
nano /etc/wheatley/secrets.env
```
Fill in:
- `TELEGRAM_BOT_TOKEN` & `TELEGRAM_USER_ID`
- `GEMINI_API_KEY`
- Your Proxmox node tokens and TLS fingerprints.

### Step 5: Configure Nodes & Checks
Copy the generic example configuration to `/etc/wheatley/config.yaml`:
```bash
cp config.example.yaml /etc/wheatley/config.yaml
nano /etc/wheatley/config.yaml
```
Verify that the configuration parses cleanly:
```bash
wheatley config validate
```

### Step 6: Enable & Start the Daemon
Enable the systemd service to start Wheatley on container boot:
```bash
systemctl enable --now wheatley
```

---

## 🛠️ CLI & Telegram Usage

### CLI Commands
Available both inside the LXC container and from the Proxmox host via the convenience wrapper:

```bash
# Auto-discover homelab services & guests across all Proxmox nodes
wheatley discover

# Export recommended draft configuration
wheatley discover --output /etc/wheatley/config.draft.yaml

# Validate configuration and secrets
wheatley config validate

# Check engine utilities
wheatley check list
wheatley check run node-health
wheatley check run-all

# Ask Wheatley AI a homelab administration question directly
wheatley ask "How is the cluster doing?"

# View LLM token activity telemetry
wheatley usage

# Inspect the SQLite audit log
wheatley audit

# Inspect the last 25 audit events
wheatley audit -n 25

# Show CLI help
wheatley --help
```

### Telegram Bot Commands
From your authorized Telegram chat:
- **Direct AI Chat** — Message Wheatley directly (e.g. *"Why is Frigate slow?"* or *"What guests are running on cave?"*) for grounded, real-time homelab analysis with tool-assisted diagnosis across any configured LLM.
- `/status` — Aggregates live cluster telemetry (CPU, RAM, uptime, and running/stopped guests across all configured nodes).
- `/checks` — Displays real-time status of all monitored checks and active mute windows.
- `/usage` — Displays current month & daily LLM token consumption and activity.
- `/investigate <id>` — Triggers automated root-cause diagnosis on any service check.
- `/mute <check_id> [mins]` — Temporarily silences alerts for a service (e.g. `/mute plex 120`).
- `/unmute <check_id>` — Re-enables alerts for a silenced check.
- `/discover` — Probes nodes & guests to auto-detect active services and container environments.
- `/audit` — Views recent security, webhook, and operational audit events.
- `/help` — Displays command cheat sheet and operational guidelines.

---

## 🗺️ Project Roadmap

- [x] **Phase 1: Foundation**
  - Unprivileged Debian 13 LXC provisioning script (`install/create-lxc.sh`).
  - Configuration loader with Pydantic schema and `${ENV_VAR}` interpolation.
  - SQLite audit logger (`audit_log`, `proposals`, `budget_usage`, `credit_ledger`).
  - Telegram bot adapter with user whitelisting and single-instance atomic file lock.
  - Systemd service daemon.
- [x] **Phase 2: Multi-Node Access & Auto-Discovery**
  - Host security provisioning script (`install/host-setup.sh`).
  - PVE REST API client wrapper with TLS certificate pinning.
  - Live multi-node status aggregation (`/status`).
  - Service fingerprint database (`wheatley/discovery/services.yaml`).
  - Automated guest & service scanning (`wheatley discover` and `/discover`).
  - Draft configuration generator (`--output config.draft.yaml`).
- [x] **Phase 3: Check Engine, Alerting & Webhooks**
  - Check engine scheduler (APScheduler) with pluggable check types.
  - Built-in checks (`http`, `node_metrics`, `storage`, `guest_state`, `tcp`, `docker`).
  - Dynamic `{guest:node/vmid}` IP resolution for DHCP resilience.
  - Alert state transitions (`OK` -> `CRIT` alert, `CRIT` -> `OK` resolved) with deduplication.
  - Interactive Telegram alert notifications with inline `[🔕 Mute 1h]` / `[🔕 Mute 24h]` buttons.
  - Telegram commands: `/checks`, `/mute`, `/unmute`.
  - CLI check utilities: `wheatley check list`, `wheatley check run <id>`, `wheatley check run-all`.
  - Proxmox VE notification webhook receiver (`POST /webhook/pve` on port 8088).
- [x] **Phase 4: LLM-Agnostic Reasoning Engine & Tool Registry**
  - Pluggable provider architecture (`BaseLLMProvider`) supporting native Google Gemini and OpenAI-compatible endpoints (Ollama, OpenAI, Groq, DeepSeek, vLLM, LocalAI).
  - Multi-turn function-calling reasoning loop using dynamic live cluster inventory instructions.
  - Tool registry enabling LLMs to query cluster telemetry, guest details, check states, and audit logs.
  - Token activity telemetry with no artificial billing caps or execution pauses.
  - Conversational AI chat in Telegram and CLI (`wheatley ask`).
  - Inline `[🔍 Investigate]` alert button and `/investigate` command for automated root-cause analysis.
- [ ] **Phase 5: Secure Remote Terminal Diagnostics**
  - SSH wrapper with strict command allowlist.
- [ ] **Phase 6: Interactive Approval Gate for Mutations**
  - Cryptographic action hashing, 15-minute expiry, snapshot-before-change prompt.
- [ ] **Phase 7: Cloud Run Watchdog**
  - Dead-man's switch heartbeat on GCP Cloud Run + Cloud Scheduler.

---

## 📄 License
MIT License. Built for the homelab community.
