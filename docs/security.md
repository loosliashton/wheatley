# Wheatley Security & Safety Model

Wheatley is designed from the ground up for safe, resilient homelab automation. It provides powerful AI-assisted diagnostic and administration capabilities while strictly preventing accidental destruction, privilege escalation, or unauthorized access.

---

## 1. Threat Model & Sandboxing

Wheatley employs defense-in-depth across the operating system, container runtime, API layer, and network boundaries:

1. **Unprivileged Dedicated LXC Container (`CT 110`):**
   - Wheatley runs inside an unprivileged Debian container provisioned via `install/create-lxc.sh`.
   - Root inside the container maps to unprivileged UIDs (`100000+`) on the Proxmox host kernel, preventing host root breakouts or direct block device manipulation.
   - The systemd service runs under a dedicated, unprivileged system account (`User=wheatley`, `Group=wheatley`) inside the container.

2. **Least-Privilege PVE API Tokens:**
   - Wheatley connects to Proxmox VE nodes using native `@pve` API tokens created via `install/host-setup.sh`.
   - The token is granted only the `PVEAuditor` role at `/`. It can read node telemetry, guest configs, storage pools, and task logs, but **cannot** create, delete, or reconfigure VMs or storage through the API.
   - API calls enforce SHA-256 TLS certificate fingerprint pinning (`tls_fingerprint`), protecting against LAN man-in-the-middle attacks even with self-signed cluster certificates.

3. **Restricted Host Shell Wrapper (`wheatley-shell`):**
   - Host SSH access is locked to `/usr/local/bin/wheatley-shell` installed by `install/host-setup.sh`.
   - Interactive shell sessions (PTY allocation, bash prompts) are strictly disabled.
   - Commands must be passed non-interactively via SSH (`SSH_ORIGINAL_COMMAND`).

4. **Narrow Sudoers Permissions:**
   - Sudoers rules in `/etc/sudoers.d/wheatley` on monitored hosts are strictly restricted to read-only diagnostics, guest exec wrappers, and non-destructive guest lifecycle operations:
     ```sudoers
     wheatley ALL=(ALL) NOPASSWD: \
         /usr/sbin/pct exec *, \
         /usr/sbin/pct start *, \
         /usr/sbin/pct stop *, \
         /usr/sbin/pct restart *, \
         /usr/sbin/pct reboot *, \
         /usr/sbin/pct shutdown *, \
         /usr/sbin/qm guest exec *, \
         /usr/sbin/qm start *, \
         /usr/sbin/qm stop *, \
         /usr/sbin/qm reboot *, \
         /usr/sbin/qm shutdown *, \
         /usr/sbin/qm reset *, \
         /usr/sbin/pvesm status*, \
         /usr/sbin/pvesm list*, \
         /usr/sbin/zpool status*, \
         /usr/sbin/zpool list*, \
         /usr/sbin/smartctl *, \
         /usr/bin/sensors *, \
         /bin/systemctl status *, \
         /usr/bin/systemctl status *, \
         /bin/systemctl is-active *, \
         /usr/bin/systemctl is-active *, \
         /bin/systemctl is-failed *, \
         /usr/bin/systemctl is-failed *, \
         /bin/journalctl *, \
         /usr/bin/journalctl *, \
         /bin/df *, \
         /usr/bin/df *, \
         /usr/bin/uptime, \
         /usr/bin/free *
     ```
   - Wheatley has no arbitrary root execution rights or destructive VM/CT deletion rights on the Proxmox host.

5. **Self-Guest Protection:**
   - The `self_guest` configuration parameter (e.g. `self_guest: "pve-node-01/110"`) protects Wheatley's own container.
   - Wheatley refuses to stop, restart, or execute mutating actions against its own container, preventing accidental self-termination.

---

## 2. Interactive Approval Gate for Mutating Actions

Wheatley divides all host and guest actions into two strictly enforced categories:

### A. Read-Only Diagnostics (Automatic)
Safe, non-destructive diagnostic commands defined in `allowlist` (e.g. `uptime`, `df -h`, `top -b -n 1`, `docker ps`, `systemctl status *`, `zpool status`, `smartctl -H *`) execute immediately when the LLM needs telemetry to diagnose an issue.

### B. Mutating Operations (Human-in-the-Loop)
Any action that alters system state (such as restarting guests, restarting system services, terminating containers, or altering system files) **strictly requires explicit human approval**:

1. The LLM prepares an execution proposal with an explanation and exact command line.
2. Wheatley renders interactive Telegram buttons:
   - **[⚡ Run Once]**: Grants one-time execution for this turn.
   - **[🛡 Always Allow on <target>]**: Permanently adds the rule to the target-scoped allowlist in SQLite.
   - **[❌ Deny]**: Halts the proposed action immediately.
3. **Automatic Expiration:** Proposals expire automatically after 15 minutes if not approved.
4. **Post-Execution Reporting:** If approved, Wheatley executes the command via `ShellExecutor` and reports the stdout/stderr back into the conversation for continued AI synthesis.

---

## 3. Dynamic Allowlist & Execution Halting

When the LLM encounters a diagnostic command not present on the default allowlist:

1. **Immediate Execution Halting (`CommandExecutionHalted`):**
   Wheatley halts reasoning immediately instead of entering continuous tool loops or hallucinating outputs, saving LLM tokens.
2. **Interactive Allowlist Card:**
   Wheatley delivers the proposal card directly to Telegram, allowing the administrator to inspect the proposed command.
3. **Target-Scoped Permissions:**
   Approved commands can be scoped strictly to the specific guest or node (`pve-node-01/100`), preventing unintended execution on other nodes.
4. **Allowlist Management:**
   - `/allowlist` — Lists all active custom rules, target scopes, and approval timestamps.
   - `/allowlist revoke <id>` — Immediately removes a custom allowlist rule from the SQLite database.

---

## 4. Configuration Isolation & Enforced `checks.yaml`

To protect cluster infrastructure and credentials, Wheatley strictly separates static cluster configuration from dynamic health checks:

1. **Immutable Core Configuration (`/etc/wheatley/config.yaml`):**
   - Contains node endpoints, API tokens, SSH keys, Telegram bot tokens, and LLM provider credentials.
   - Owned by root with read-only permissions for Wheatley (`chmod 644`).
   - Defining `checks:` directly inside `config.yaml` is prohibited by the configuration loader.

2. **Dedicated Checks File (`/etc/wheatley/checks.yaml`):**
   - All service, container, storage, and node health checks live in `/etc/wheatley/checks.yaml`.
   - Referenced via `checks_file: "/etc/wheatley/checks.yaml"` in `config.yaml` (or loaded automatically if present alongside `config.yaml`).

3. **Conversational Tuning with Atomic Backups:**
   - Administrators can tune checks conversationally in Telegram:
     > *"Wheatley, ignore the stopped backup-cron container in docker checks."*
   - Wheatley generates a unified diff using `CheckConfigEditor` and presents an approval card.
   - When approved, Wheatley creates an atomic timestamped backup in `/var/lib/wheatley/backups/`, safely applies the change, and hot-reloads the check engine live without restarting the daemon.

---

## 5. Secret Isolation & Zero Hardcoding

1. **Dedicated Secrets File (`/etc/wheatley/secrets.env`):**
   - All API keys, Telegram tokens, PVE tokens, and watchdog URLs are stored in `/etc/wheatley/secrets.env`.
   - Restricted to `chmod 600` owned by `wheatley:wheatley`.
   - Template provided in `secrets.example.env`.

2. **Environment Variable Interpolation:**
   - Configuration files use `${ENV_VAR}` or `${ENV_VAR:-default}` syntax.
   - Secrets are loaded into process memory only at startup and never logged or written to disk.

---

## 6. Audit Logging & Security Telemetry

Every operational event is committed to a local SQLite database (`/var/lib/wheatley/wheatley.db`):

- **Audit Events (`audit_log`):** Records timestamps, user IDs, actions, target nodes/guests, commands executed, and return codes.
- **Approval History (`proposals`):** Records all proposed actions, unified diffs, approval/denial states, and approver IDs.
- **LLM Telemetry (`llm_usage`):** Tracks input/output token usage per interaction.
- **Session Privacy & Auto-Reset:** Conversation context auto-resets after inactivity (`session_timeout_minutes: 60`), and conversation history older than `history_retention_days` (default: 30) is pruned.

Administrators can inspect the audit trail at any time:
- CLI: `wheatley audit -n 50`
- Telegram: `/audit`
