# Wheatley Check Types Reference

Wheatley monitors your Proxmox VE cluster using a modular check engine. All checks are configured in `/etc/wheatley/checks.yaml` (or referenced via `checks_file:`). Custom check plugins can also be installed via Python entry points.

---

## Enforced Separation: `checks.yaml`

Checks must be defined in a dedicated checks configuration file (default: `/etc/wheatley/checks.yaml`) rather than inside `config.yaml`:

1. **Security Isolation:** `/etc/wheatley/config.yaml` (cluster nodes, tokens, bot credentials) stays immutable and read-only.
2. **Conversational Tuning:** You can ask Wheatley directly in chat:
   > *"Ignore backup-cron in the docker check"*
3. **Interactive Human Approval:** Wheatley generates a roundtrip YAML diff and presents an interactive proposal card with **[✅ Approve Update]** and **[❌ Deny]**.
4. **Zero-Downtime Hot-Reload:** When approved, Wheatley creates a backup in `/var/lib/wheatley/backups/`, applies the update atomically to `checks.yaml`, and reloads the check engine live without restarting the daemon.

---

## Built-In Check Types

### 1. `node_metrics`
Monitors CPU and RAM utilization across cluster nodes via the PVE REST API.

```yaml
- id: node-health
  type: node_metrics
  enabled: true
  interval_seconds: 60
  params:
    nodes: "*"                 # "*" for all configured nodes, or a list ["pve-node-01", "pve-node-02"]
    cpu_warn_percent: 80       # warning threshold (default: 80)
    cpu_crit_percent: 90       # critical threshold (default: 90)
    mem_warn_percent: 80       # warning threshold (default: 80)
    mem_crit_percent: 90       # critical threshold (default: 90)
```

### 2. `storage`
Monitors available capacity on local, ZFS, NFS, or Ceph storage pools.

```yaml
- id: storage-health
  type: storage
  enabled: true
  interval_seconds: 120
  params:
    nodes: "*"                 # "*" for all nodes or list of node names
    warn_percent: 85           # warning threshold (default: 85)
    crit_percent: 95           # critical threshold (default: 95)
```

### 3. `guest_state`
Verifies that VMs and LXC containers (such as those marked `onboot: 1`) are in the expected `running` state.

```yaml
- id: guests-up
  type: guest_state
  enabled: true
  interval_seconds: 60
  params:
    select:
      onboot: true             # monitor all guests configured to start on boot
    exclude:
      - "pve-node-01/999"      # optional guest specs ("<node>/<vmid>") to ignore
```

### 4. `http`
Performs HTTP/HTTPS health checks with support for status code validation, TLS verification control, response body substring matching, and `{guest:node/vmid}` dynamic IP resolution.

```yaml
- id: web-app
  type: http
  enabled: true
  interval_seconds: 60
  params:
    url: "http://{guest:pve-node-01/102}:8080/health"
    expected_status: [200]     # HTTP status code or list of codes (default: [200])
    timeout_seconds: 5         # request timeout in seconds (default: 5.0)
    tls_verify: true           # whether to verify TLS certs (default: true)
    match_body: "ok"           # optional substring requirement (alias: body_contains)
```

### 5. `tcp`
Checks raw TCP port connectivity (ideal for databases, MQTT brokers, Redis, SSH).

```yaml
- id: mqtt-tcp
  type: tcp
  enabled: true
  interval_seconds: 60
  params:
    host: "{guest:pve-node-01/105}"
    port: 1883
    timeout_seconds: 5         # timeout in seconds (default: 5)
```

### 6. `docker`
Monitors Docker container health and restart loops on a Docker host (LXC or VM) via native guest execution (`pct exec` / `qm guest exec`).

```yaml
- id: docker-containers
  type: docker
  enabled: true
  interval_seconds: 60
  params:
    guest: "pve-node-01/100"   # guest spec ("<node>/<vmid>") running Docker
    containers: "*"            # "*" for all containers or list of names
    exclude: []                # list of container names to ignore
    ignore_exited_0: true      # treat clean Exited (0) as healthy (default: true)
    alert_on:                  # container states that trigger alerts
      - exited
      - unhealthy
      - restarting
      - dead
```

### 7. `command`
Executes an allowlisted read-only diagnostic command inside a guest or on a host node and evaluates exit code and output.

```yaml
- id: snapraid-status
  type: command
  enabled: true
  interval_seconds: 3600
  params:
    guest: "pve-node-01/101"   # guest spec ("<node>/<vmid>"), or use 'node: "pve-node-01"'
    command: "snapraid status"
    timeout_seconds: 30
    expected_exit_code: 0      # expected exit code (alias: expect_exit_code)
    output_regex: "No error"   # optional regex that output must match
    fail_on_regex: "DANGER"    # optional regex that triggers failure if matched
```

### 8. `systemd`
Verifies that specific systemd units on a host node or inside a guest are `active (running)`.

```yaml
- id: docker-systemd
  type: systemd
  enabled: true
  interval_seconds: 60
  params:
    guest: "pve-node-01/107"   # guest spec ("<node>/<vmid>"), or use 'node: "pve-node-01"'
    units:
      - "docker.service"
```

### 9. `zfs`
Monitors ZFS storage pool health (`ONLINE`, `DEGRADED`, scrub errors).

```yaml
- id: zfs-pools
  type: zfs
  enabled: true
  interval_seconds: 300
  params:
    nodes: "*"                 # "*" for all nodes, node name, or list of nodes
    pool: "tank"               # optional pool name (omit or "*" for all pools)
```

### 10. `smart`
Monitors disk SMART health status (`PASSED` / `FAILED`) using `smartctl`.

```yaml
- id: smart-disks
  type: smart
  enabled: true
  interval_seconds: 3600
  params:
    node: "pve-node-01"
    devices:                   # list of device paths, or "*" to scan all drives
      - "/dev/nvme0n1"
      - "/dev/sda"
```

### 11. `lvm_thin`
Monitors LVM Thin storage pool capacity across Proxmox VE hosts via `pvesm status` (with API fallback) and alerts before data/metadata exhaustion.

```yaml
- id: lvm-thin-health
  type: lvm_thin
  enabled: true
  interval_seconds: 120
  params:
    nodes: "*"                 # "*" for all nodes or list of node names
    pool: "local-lvm"          # optional specific thin pool name
    warn_percent: 80           # warning threshold (default: 80.0)
    crit_percent: 90           # critical threshold (default: 90.0)
```

### 12. `sensors`
Monitors CPU, NVMe, and motherboard temperatures on a Proxmox VE host via `sensors -j` (`lm-sensors`).

```yaml
- id: host-temps
  type: sensors
  enabled: true
  interval_seconds: 60
  params:
    node: "pve-node-01"
    warn_temp_c: 80            # warning temperature in °C (default: 80.0)
    crit_temp_c: 90            # critical temperature in °C (default: 90.0)
```

---

## Dynamic IP Resolution: `{guest:node/vmid}`

Wheatley supports dynamic IP resolution in check URLs and hostnames:
- `{guest:pve-node-01/102}` dynamically queries Proxmox API / guest agent for the primary IPv4 address of guest `102` on node `pve-node-01`.
- This ensures your checks survive DHCP lease renewals or network subnet migrations without manually reconfiguring URLs.

---

## Writing Third-Party Check Plugins

Wheatley supports external check plugins via standard Python entry points:

1. Create a class subclassing `wheatley.checks.base.BaseCheck`.
2. Implement `async def run(self, context: CheckContext) -> CheckResult`.
3. In your `pyproject.toml`, declare the entry point:
   ```toml
   [project.entry-points."wheatley.checks"]
   my_custom_check = "my_package.checks:MyCustomCheck"
   ```
4. Wheatley will automatically discover and load your check on startup.
