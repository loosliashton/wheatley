# Wheatley Check Types Reference

Wheatley comes with built-in health check types for monitoring nodes, guests, and services. Custom checks can also be plugged in via entry points.

---

## Built-In Check Types

### 1. `node_metrics`
Monitors CPU and RAM utilization on a Proxmox node via the PVE REST API.

```yaml
- id: pve1-metrics
  type: node_metrics
  interval_seconds: 60
  params:
    node: cave
    cpu_warn_pct: 85
    cpu_crit_pct: 95
    mem_warn_pct: 90
    mem_crit_pct: 98
```

### 2. `storage`
Monitors available capacity on local, ZFS, NFS, or Ceph storage pools.

```yaml
- id: storage-local-lvm
  type: storage
  interval_seconds: 300
  params:
    node: cave
    storage: local-lvm
    used_warn_pct: 85
    used_crit_pct: 95
```

### 3. `guest_state`
Verifies that VMs and LXC containers that are configured with `onboot: 1` are running.

```yaml
- id: guest-status-all
  type: guest_state
  interval_seconds: 120
  params:
    node: cave
    ignore_vmids: [999]  # optional list of VMIDs to skip
```

### 4. `http`
Performs HTTP/HTTPS health checks with support for status code validation, response body substring matching, and `{guest:node/vmid}` dynamic IP resolution.

```yaml
- id: plex-http
  type: http
  interval_seconds: 60
  params:
    url: "http://{guest:cave/102}:32400/identity"
    expected_status: 200
    timeout_seconds: 10
    body_contains: "MediaContainer"  # optional
```

### 5. `tcp`
Checks raw TCP port connectivity (useful for databases, SSH, MQTT).

```yaml
- id: mqtt-tcp
  type: tcp
  interval_seconds: 60
  params:
    host: "{guest:cave/105}"
    port: 1883
    timeout_seconds: 5
```

### 6. `docker`
Inspects container states inside a guest (via `qm guest exec` / `pct exec` or Docker API).
Automatically treats cleanly stopped containers (`Exited (0)`) as healthy unless `ignore_exited_0` is set to `false`.

```yaml
- id: docker-containers
  type: docker
  interval_seconds: 60
  params:
    node: cave
    vmid: 101
    ignore_exited_0: true  # default: true, skips dormant containers
    ignore_containers: ["temp-worker", "backup-cron"]
```

### 7. `command`
Executes an arbitrary read-only diagnostic command inside a guest or on a host and evaluates the exit code and output.

```yaml
- id: snapraid-status
  type: command
  interval_seconds: 3600
  params:
    node: cave
    vmid: 101
    command: "snapraid status"
    timeout_seconds: 30
```

---

## Dynamic IP Resolution: `{guest:node/vmid}`

Wheatley supports dynamic IP resolution in check URLs and hostnames:
- `{guest:cave/102}` dynamically resolves to the primary IPv4 address of guest `102` on node `cave`.
- This ensures your checks survive DHCP lease renewals or network reconfiguration.

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
