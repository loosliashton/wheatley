# Wheatley Security & Safety Model

Wheatley is designed from the ground up for safe, resilient homelab automation. It provides powerful AI-assisted diagnostic and administration capabilities while strictly preventing accidental destruction, privilege escalation, or unauthorized access.

---

## 1. Threat Model & Trust Boundaries

Wheatley separates its security controls into two distinct layers:
- **LLM Safety Guardrails (Application Layer):** The read-only command allowlist, `self_guest` protection, and the interactive Telegram **[Approve] / [Deny]** gate are enforced by Wheatley's Python process (`AgentToolbox` and `ProposalOrchestrator`) to constrain what the AI model can run without human consent.
- **Host & Network Hardening (OS Layer):** Container isolation, read-only Proxmox API tokens, SSH key restrictions, and scoped host `sudoers` rules limit blast radius on the Proxmox host itself.

> [!IMPORTANT]
> **Treat the Wheatley Container as a High-Value Control Node**
> Because Wheatley needs to run diagnostics and approved actions across your guests, its SSH key (`/var/lib/wheatley/.ssh/id_ed25519`) is granted `sudo /usr/sbin/pct exec *` and `sudo /usr/sbin/qm guest exec *` on monitored Proxmox nodes. While the interactive approval gate is enforced by Wheatley's code against the LLM, **anyone who gains root access to the Wheatley container or exfiltrates its SSH private key effectively has root inside every LXC container and QEMU VM** (as well as power control over guests). Secure the Wheatley LXC with the same care as an Ansible control node or management bastion.

### Host & Container Hardening Layers

1. **Unprivileged Dedicated LXC Container (`CT 110`):**
   - Wheatley runs inside an unprivileged Debian container provisioned via `install/create-lxc.sh`.
   - Root inside the container maps to unprivileged UIDs (`100000+`) on the Proxmox host kernel, preventing container-to-host kernel breakouts or direct block device manipulation.
   - The systemd service and CLI wrapper run under a dedicated system account (`User=wheatley`, `Group=wheatley`) inside the container.

2. **Least-Privilege PVE API Tokens:**
   - Wheatley connects to Proxmox VE nodes using native `@pve` API tokens created via `install/host-setup.sh`.
   - The token is granted only the `PVEAuditor` role at `/`. It can read node telemetry, guest configs, storage pools, and task logs, but **cannot** create, delete, or reconfigure VMs or storage through the REST API.
   - API calls enforce SHA-256 TLS certificate fingerprint pinning (`tls_fingerprint`). SSH connections use Trust-On-First-Use (TOFU) host key pinning in `/var/lib/wheatley/.ssh/known_hosts`: the first connection to a node accepts and records the server's public key, and all subsequent connections verify against that pinned key.

3. **Non-Interactive Host SSH Wrapper (`wheatley-shell`) & Key Restrictions:**
   - The `wheatley` user on Proxmox hosts uses `/usr/local/bin/wheatley-shell` (installed by `install/host-setup.sh`) and `no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty` in `/home/wheatley/.ssh/authorized_keys`.
   - Interactive shell logins, PTY allocation, and SSH port/SOCKS forwarding are disabled.
   - Guest execution commands (`sudo /usr/sbin/pct exec <vmid> -- /bin/bash -c <arg>` and `sudo /usr/sbin/qm guest exec <vmid> -- /bin/bash -c <arg>`) must match that exact form with a single quoted argument and no trailing tokens.
   - Direct host commands may use pipes and standard redirections, while `wheatley-shell` rejects multiline input, command chaining (`;`, `&&`, `||`, background `&`), command/process substitution (`$(...)`, backticks, `<(...)`, `>(...)`), ANSI-C quoting (`$'...'`), `pct/qm destroy`, and interactive `sudo` shell flags (`sudo -i`, `sudo -s`, `sudo su`, `sudo bash`).
   - Note that `wheatley-shell` is a defense-in-depth wrapper, not a privilege boundary: root privileges on the Proxmox host are governed exclusively by `/etc/sudoers.d/wheatley`, and which commands the LLM may run without human consent is governed by Wheatley's application allowlist.

4. **Scoped Host Sudoers Permissions (`/etc/sudoers.d/wheatley`):**
   - On the **Proxmox host OS**, `sudo` is restricted to specific diagnostic tools, guest power commands, and guest exec wrappers:
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
   - The `wheatley` user cannot `sudo` arbitrary root commands on the **Proxmox host OS** itself, nor can it run `pct destroy` or `qm destroy`.
   - **Guest Scope Note:** `pct exec *` and `qm guest exec *` intentionally grant root command execution **inside every guest VM and container** on the node so Wheatley can perform in-guest diagnostics and approved remediations. The boundary between read-only diagnostics and mutating commands inside guests is enforced by Wheatley's application-layer allowlist and approval gate.

5. **Self-Guest Protection:**
   - The `self_guest` configuration parameter (e.g. `self_guest: "pve-node-01/110"`) protects Wheatley's own container.
   - Wheatley refuses to propose or execute mutating actions targeting `self_guest`—whether addressed directly (`pve-node-01/110`) or via host-level `pct`/`qm`/`pvesh` commands referencing its VMID—preventing accidental self-termination.

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
