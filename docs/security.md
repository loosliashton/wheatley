# Wheatley Security & Safety Model

Wheatley is designed from the ground up for safe homelab automation. It provides powerful diagnostic capabilities while preventing accidental destruction or unauthorized access.

---

## 1. Threat Model & Sandboxing

1. **Unprivileged Dedicated LXC:**
   Wheatley runs inside an unprivileged Debian container (`CT 110`). Root inside the container maps to unprivileged UID `100000+` on the host, preventing host root escalation.

2. **Least Privilege PVE Tokens:**
   Wheatley connects to Proxmox VE using an API token with `PVEAuditor` role on `/`. It can read node telemetry, guest configs, and storage states, but **cannot** create, delete, or reconfigure VMs via the API.

3. **Restricted Shell Wrapper (`wheatley-shell`):**
   SSH access from Wheatley to the Proxmox host is locked to `install/wheatley-shell`. It enforces command validation, strips harmful syntax, and prevents interactive shell sessions.

---

## 2. Interactive Approval Gate for Mutating Actions

Wheatley divides all actions into two categories:

### A. Read-Only Diagnostics (Automatic)
Safe commands (e.g. `uptime`, `df -h`, `smartctl --scan`, `docker ps`, `systemctl list-units`) run without user intervention when the LLM is diagnosing an issue.

### B. Mutating Operations (Human-in-the-Loop)
Any action that modifies system state (e.g. `pct restart <vmid>`, `systemctl restart <unit>`, container deletions) **strictly requires explicit approval**.
1. The LLM prepares an action proposal (`ACTION PROPOSAL REQUIRED`).
2. Wheatley renders interactive Telegram buttons: **[✅ Approve]** and **[❌ Deny]**.
3. Proposals expire automatically after 15 minutes.
4. If approved, Wheatley executes the command and reports the result back into the chat.
5. If denied, Wheatley halts the action and gracefully summarizes its findings.

---

## 3. Dynamic Allowlist & Halting

If the LLM determines it needs to run a diagnostic command that is not yet on the default allowlist:
1. **Immediate Execution Halting:** The agent stops immediately without wasting additional reasoning tokens.
2. **Interactive Allowlist Proposal:** A card is delivered with three options:
   - **[⚡ Run Once]**: Grants one-time execution permission for this turn.
   - **[🛡 Always Allow on <target>]**: Permanently adds the validated rule to the SQLite allowlist for that target.
   - **[❌ Deny]**: Rejects the command and continues without it.
3. Users can inspect and revoke rules at any time using:
   - `/allowlist`: Lists active custom rules.
   - `/allowlist revoke <id>`: Revokes a specific rule.

---

## 4. Secret Isolation & Zero Hardcoding

1. **Secrets File (`/etc/wheatley/secrets.env`):**
   Stores all tokens, passwords, and API keys with permissions `0600` owned by `wheatley:wheatley`.
2. **Config Interpolation:**
   `/etc/wheatley/config.yaml` uses `${ENV_VAR}` syntax. No secrets are ever committed to git.
3. **Audit Logging:**
   Every user interaction, webhook, and command execution is logged to a local SQLite database (`/var/lib/wheatley/wheatley.db`) with timestamp, user ID, target, and execution result.
