# Wheatley Check Types Reference

Wheatley monitors your Proxmox VE cluster using a modular check engine. All checks are configured in `/etc/wheatley/checks.yaml` (or referenced via `checks_file:`). Custom check plugins can also be installed via Python entry points.

---

## Enforced Separation: `checks.yaml`

Checks must be defined in a dedicated checks configuration file (default: `/etc/wheatley/checks.yaml`) rather than inside `config.yaml`:

1. **Security Isolation:** `/etc/wheatley/config.yaml` (cluster nodes, tokens, bot credentials) stays immutable and read-only.
2. **Conversational Tuning:** You can ask Wheatley directly in chat:
   > *"Ignore seedboxapi in the docker check"*
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
    nodes: "*"                 # "*" for all configured nodes, or a list ["cave", "doug"]
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
      - "cave/999"             # optional guest specs ("<node>/<vmid>") to ignore
```

### 4. `http`
Performs HTTP/HTTPS health checks with support for status code validation, TLS verification control, response body substring matching, and `{guest:node/vmid}` dynamic IP resolution.

```yaml
- id: plex
  type: http
  enabled: true
  interval_seconds: 60
  params:
    url: "http://{guest:cave/102}:32400/identity"
    expected_status: 200       # HTTP status code (default: 200)
    timeout_seconds: 10        # request timeout in seconds (default: 10)
    tls_verify: true           # whether to verify TLS certs (default: true)
    body_contains: "MediaContainer"  # optional substring requirement
```

### 5. `tcp`
Checks raw TCP port connectivity (ideal for databases, MQTT brokers, Redis, SSH).

```yaml
- id: mqtt-tcp
  type: tcp
  enabled: true
  interval_seconds: 60
  params:
    host: "{guest:cave/105}"
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
    guest: "cave/100"          # guest spec ("<node>/<vmid>") running Docker
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
Executes an arbitrary read-only diagnostic command inside a guest or on a host node and evaluates exit code and output.

```yaml
- id: snapraid-status
  type: command
  enabled: true
  interval_seconds: 3600
  params:
    guest: "cave/101"          # guest spec or host node name
    command: "snapraid status"
    timeout_seconds: 30
    expect_exit_code: 0
```

### 8. `systemd`
Verifies that specific systemd units on a host node or inside a guest are `active (running)`.

```yaml
- id: frigate-systemd
  type: systemd
  enabled: true
  interval_seconds: 60
  params:
    guest: "cave/107"
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
    node: "cave"
    pools: "*"                 # "*" for all pools or list of pool names
```

### 10. `smart`
Monitors disk SMART health status (`PASSED` / `FAILED`) using `smartctl`.

```yaml
- id: smart-disks
  type: smart
  enabled: true
  interval_seconds: 3600
  params:
    node: "cave"
    devices: "*"               # scan all devices or specify ["/dev/sda", "/dev/nvme0n1"]
```

---

## Dynamic IP Resolution: `{guest:node/vmid}`

Wheatley supports dynamic IP resolution in check URLs and hostnames:
- `{guest:cave/102}` dynamically queries Proxmox API / guest agent for the primary IPv4 address of guest `102` on node `cave`.
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
