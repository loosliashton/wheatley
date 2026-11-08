#!/usr/bin/env bash
# ==============================================================================
# Wheatley Homelab Agent - LXC Provisioning Script
# ==============================================================================
# Creates an unprivileged Debian LXC container on Proxmox VE for running
# the Wheatley monitoring agent.
#
# Usage:
#   ./create-lxc.sh [OPTIONS]
#
# Options:
#   --ctid <id>          Container ID (default: 110 or next free)
#   --hostname <name>    Hostname (default: wheatley)
#   --ip <ip/cidr>       Static IP with CIDR (default: 192.168.1.110/24)
#   --gateway <ip>       Gateway IP (default: 192.168.1.1)
#   --bridge <bridge>    Network bridge (default: vmbr0)
#   --storage <storage>  Storage pool (default: local-lvm)
#   --disk-size <size>   Disk size (default: 8G)
#   --cores <n>          CPU cores (default: 1)
#   --memory <mb>        RAM in MB (default: 1024)
#   --swap <mb>          Swap in MB (default: 512)
#   --template <tmpl>    Template name or storage:vztmpl/... (auto-detected if omitted)
#   --dns <servers>      Nameservers (default: 1.1.1.1,8.8.8.8)
#   --start              Start container after creation (default: true)
#   --help, -h           Show this help message
# ==============================================================================

set -euo pipefail

# Defaults
CTID=""
HOSTNAME="wheatley"
IP="192.168.1.110/24"
GATEWAY="192.168.1.1"
BRIDGE="vmbr0"
STORAGE="local-lvm"
DISK_SIZE="8G"
CORES=1
MEMORY=1024
SWAP=512
TEMPLATE=""
DNS="1.1.1.1 8.8.8.8"
START=true

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOSTNAME="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --disk-size) DISK_SIZE="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --swap) SWAP="$2"; shift 2 ;;
    --template) TEMPLATE="$2"; shift 2 ;;
    --dns) DNS="$2"; shift 2 ;;
    --no-start) START=false; shift 1 ;;
    -h|--help)
      grep '^# ' "$0" | cut -c 3-
      exit 0
      ;;
    *)
      echo "Error: Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Ensure running on Proxmox host
if ! command -v pct >/dev/null 2>&1; then
  echo "Error: 'pct' command not found. This script must be run directly on a Proxmox VE host." >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Error: This script must be run as root." >&2
  exit 1
fi

# Auto-determine CTID if not specified
if [[ -z "$CTID" ]]; then
  if ! pct status 110 >/dev/null 2>&1 && ! qm status 110 >/dev/null 2>&1; then
    CTID=110
  else
    CTID=$(pvesh get /cluster/nextid)
  fi
fi

# Check if CTID already exists
if pct status "$CTID" >/dev/null 2>&1 || qm status "$CTID" >/dev/null 2>&1; then
  echo "Error: VM/CT ID $CTID is already in use." >&2
  exit 1
fi

# Auto-determine host architecture
if command -v dpkg >/dev/null 2>&1; then
  HOST_ARCH="$(dpkg --print-architecture)"
else
  case "$(uname -m)" in
    x86_64) HOST_ARCH="amd64" ;;
    aarch64|arm64) HOST_ARCH="arm64" ;;
    i386|i686) HOST_ARCH="i386" ;;
    *) HOST_ARCH="$(uname -m)" ;;
  esac
fi

# Auto-detect template if not provided
if [[ -z "$TEMPLATE" ]]; then
  echo "Detecting available Debian template on storage for architecture '${HOST_ARCH}'..."
  # Prioritize Debian 13, then Debian 12 matching host architecture
  for tmpl in $(pveam list local 2>/dev/null | awk '{print $1}' | grep -E 'debian-(13|12)-standard' | grep "_${HOST_ARCH}\." | sort -rV); do
    TEMPLATE="$tmpl"
    break
  done

  if [[ -z "$TEMPLATE" ]]; then
    echo "No local Debian 12 or 13 template found for architecture '${HOST_ARCH}'. Checking downloadable templates..."
    pveam update >/dev/null 2>&1 || true
    AVAILABLE_TMPL=$(pveam available | awk '{print $2}' | grep -E 'debian-(13|12)-standard' | grep "_${HOST_ARCH}\." | sort -rV | head -n1)
    if [[ -n "$AVAILABLE_TMPL" ]]; then
      echo "Downloading template $AVAILABLE_TMPL to local..."
      pveam download local "$AVAILABLE_TMPL"
      TEMPLATE="local:vztmpl/$AVAILABLE_TMPL"
    else
      echo "Error: Could not find or download a Debian template for architecture '${HOST_ARCH}'." >&2
      exit 1
    fi
  fi
fi

echo "================================================="
echo " Provisioning Wheatley LXC (CTID: $CTID)"
echo " Hostname:    $HOSTNAME"
echo " IP:          $IP"
echo " Gateway:     $GATEWAY"
echo " Bridge:      $BRIDGE"
echo " Storage:     $STORAGE (${DISK_SIZE})"
echo " Resources:   ${CORES} Core(s), ${MEMORY} MB RAM, ${SWAP} MB Swap"
echo " Template:    $TEMPLATE"
echo "================================================="

# Format disk size for pct create (needs integer GiB, e.g. 8)
DISK_SIZE_G="${DISK_SIZE%[Gg]}"

# Create unprivileged container
pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --ostype debian \
  --cores "$CORES" \
  --memory "$MEMORY" \
  --swap "$SWAP" \
  --storage "$STORAGE" \
  --rootfs "${STORAGE}:${DISK_SIZE_G}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=${IP},gw=${GATEWAY},type=veth" \
  --nameserver "$DNS" \
  --onboot 1 \
  --unprivileged 1 \
  --features nesting=1

echo "Container $CTID created successfully."

if [[ "$START" == "true" ]]; then
  echo "Starting container $CTID..."
  pct start "$CTID"

  echo "Waiting for container network connectivity..."
  for i in {1..30}; do
    if pct exec "$CTID" -- ping -c1 -W1 1.1.1.1 >/dev/null 2>&1; then
      echo "Network is up inside CT $CTID."
      break
    fi
    sleep 1
  done

  echo "Updating container package lists and installing prerequisites..."
  pct exec "$CTID" -- apt-get update
  pct exec "$CTID" -- apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    ca-certificates \
    jq \
    sudo \
    sqlite3 \
    openssh-client

  echo "Setting up wheatley system user and directories..."
  pct exec "$CTID" -- bash -c '
    if ! id -u wheatley >/dev/null 2>&1; then
      useradd -r -m -d /var/lib/wheatley -s /bin/bash wheatley
    fi
    echo "precedence ::ffff:0:0/96 100" >> /etc/gai.conf
    mkdir -p /opt/wheatley /etc/wheatley /var/lib/wheatley /var/log/wheatley
    chown -R wheatley:wheatley /opt/wheatley /var/lib/wheatley /var/log/wheatley
    chown root:wheatley /etc/wheatley
    chmod 750 /etc/wheatley
  '

  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [[ -f "${REPO_DIR}/pyproject.toml" ]]; then
    echo "Copying Wheatley source tree into CT $CTID:/opt/wheatley..."
    tar -C "$REPO_DIR" \
      --exclude='./venv' \
      --exclude='./.venv' \
      --exclude='./__pycache__' \
      --exclude='./.pytest_cache' \
      -cf - . | pct exec "$CTID" -- tar -C /opt/wheatley -xf -
  else
    echo "Cloning Wheatley repository into CT $CTID:/opt/wheatley..."
    pct exec "$CTID" -- git clone https://github.com/loosliashton/wheatley.git /opt/wheatley
  fi

  echo "Installing Wheatley Python environment and systemd service inside CT $CTID..."
  pct exec "$CTID" -- bash -c '
    chown -R wheatley:wheatley /opt/wheatley
    git config --system --add safe.directory /opt/wheatley || true
    su -s /bin/bash wheatley -c "python3 -m venv /opt/wheatley/venv && /opt/wheatley/venv/bin/pip install --upgrade pip && /opt/wheatley/venv/bin/pip install -e /opt/wheatley"
    # Install container-side wrapper ensuring CLI commands run as wheatley user even if invoked as root.
    # NOTE: this block is inside a single-quoted bash -c string, so the heredoc delimiter must be
    # double-quoted (single quotes would terminate the outer string and $EUID/"$@" would be expanded).
    cat <<"WRAPPER_EOF" > /usr/local/bin/wheatley
#!/usr/bin/env bash
if [[ $EUID -eq 0 ]]; then
  exec runuser -u wheatley -- /opt/wheatley/venv/bin/wheatley "$@"
else
  exec /opt/wheatley/venv/bin/wheatley "$@"
fi
WRAPPER_EOF
    chmod 755 /usr/local/bin/wheatley
    ln -sf /usr/local/bin/wheatley /usr/bin/wheatley
    chown -R wheatley:wheatley /var/lib/wheatley

    # Seed configuration templates if not already present
    if [[ ! -f /etc/wheatley/config.yaml && -f /opt/wheatley/config.example.yaml ]]; then
      cp /opt/wheatley/config.example.yaml /etc/wheatley/config.yaml
      chown root:wheatley /etc/wheatley/config.yaml
      chmod 640 /etc/wheatley/config.yaml
    fi
    if [[ ! -f /etc/wheatley/checks.yaml && -f /opt/wheatley/checks.example.yaml ]]; then
      cp /opt/wheatley/checks.example.yaml /etc/wheatley/checks.yaml
      chown wheatley:wheatley /etc/wheatley/checks.yaml
      chmod 640 /etc/wheatley/checks.yaml
    fi
    if [[ ! -f /etc/wheatley/secrets.env && -f /opt/wheatley/secrets.example.env ]]; then
      cp /opt/wheatley/secrets.example.env /etc/wheatley/secrets.env
      chown wheatley:wheatley /etc/wheatley/secrets.env
      chmod 600 /etc/wheatley/secrets.env
    fi
    # Ensure a random webhook secret exists (fresh installs and upgrades of older secrets.env files)
    if [[ -f /etc/wheatley/secrets.env ]]; then
      RAND_WEBHOOK_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))" 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d " \n")
      if grep -q "^WEBHOOK_SECRET_TOKEN=" /etc/wheatley/secrets.env; then
        sed -i "s/^WEBHOOK_SECRET_TOKEN=\"\{0,1\}\(change-me-to-a-random-secret-string\)\{0,1\}\"\{0,1\}$/WEBHOOK_SECRET_TOKEN=\"${RAND_WEBHOOK_TOKEN}\"/" /etc/wheatley/secrets.env
      else
        printf "\n# Webhook receiver shared secret (auto-generated)\nWEBHOOK_SECRET_TOKEN=\"%s\"\n" "$RAND_WEBHOOK_TOKEN" >> /etc/wheatley/secrets.env
      fi
    fi

    # Install systemd service unit
    if [[ -f /opt/wheatley/deploy/wheatley.service ]]; then
      cp /opt/wheatley/deploy/wheatley.service /etc/systemd/system/wheatley.service
      systemctl daemon-reload
      systemctl enable wheatley
    fi

    # Generate SSH keypair for host/guest diagnostics
    su -s /bin/bash wheatley -c "mkdir -p /var/lib/wheatley/.ssh && chmod 700 /var/lib/wheatley/.ssh && if [[ ! -f /var/lib/wheatley/.ssh/id_ed25519 ]]; then ssh-keygen -t ed25519 -N \"\" -C \"wheatley@ct-${HOSTNAME}\" -f /var/lib/wheatley/.ssh/id_ed25519 >/dev/null; fi"
  '

  PUBKEY=$(pct exec "$CTID" -- cat /var/lib/wheatley/.ssh/id_ed25519.pub 2>/dev/null || true)

  # Authorize SSH key on local Proxmox host if wheatley user exists (with forwarding and PTY disabled).
  # Any existing entry for the same key (e.g. an unrestricted line from an older install) is replaced.
  if [[ -n "$PUBKEY" ]] && id -u wheatley >/dev/null 2>&1; then
    SSH_KEY_OPTS="no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"
    KEY_BLOB=$(printf '%s\n' "$PUBKEY" | awk '{print $2}')
    AK_FILE="/home/wheatley/.ssh/authorized_keys"
    mkdir -p /home/wheatley/.ssh
    chmod 700 /home/wheatley/.ssh
    touch "$AK_FILE"
    AK_TMP=$(mktemp "${AK_FILE}.XXXXXX")
    if [[ -n "$KEY_BLOB" ]]; then
      grep -vF -- "$KEY_BLOB" "$AK_FILE" > "$AK_TMP" || true
    else
      cat "$AK_FILE" > "$AK_TMP"
    fi
    printf '%s %s\n' "$SSH_KEY_OPTS" "$PUBKEY" >> "$AK_TMP"
    mv "$AK_TMP" "$AK_FILE"
    chmod 600 "$AK_FILE"
    chown -R wheatley:wheatley /home/wheatley/.ssh
    echo "Authorized CT $CTID SSH key for user 'wheatley' on local Proxmox host."
  fi

  # Install host-side convenience wrapper to access Wheatley CLI from the Proxmox host.
  # Never overwrite an unrelated /usr/local/bin/wheatley; only (re)write our own wrapper.
  HOST_WRAPPER="/usr/local/bin/wheatley"
  HOST_WRAPPER_MARKER="# wheatley-host-cli-wrapper"
  if [[ -e "$HOST_WRAPPER" ]] && ! grep -qF "$HOST_WRAPPER_MARKER" "$HOST_WRAPPER" 2>/dev/null; then
    echo "Note: $HOST_WRAPPER already exists and was not created by this script; leaving it untouched."
    echo "      Use 'pct exec ${CTID} -- wheatley <cmd>' to run the CLI from the host."
  else
    cat <<HOST_WRAPPER_EOF > "$HOST_WRAPPER"
#!/usr/bin/env bash
${HOST_WRAPPER_MARKER}
# Forwards Wheatley CLI commands into the Wheatley container.
# Override the target container with WHEATLEY_CTID=<id> if needed.
exec pct exec "\${WHEATLEY_CTID:-${CTID}}" -- /usr/local/bin/wheatley "\$@"
HOST_WRAPPER_EOF
    chmod 755 "$HOST_WRAPPER"
    echo "Installed host CLI convenience wrapper at $HOST_WRAPPER (forwards to CT ${CTID})."
  fi

  echo ""
  echo "================================================="
  echo " Wheatley LXC provisioning complete!"
  echo " CTID: $CTID is running at $IP"
  echo "================================================="
  if [[ -n "$PUBKEY" ]]; then
    echo "Agent SSH Public Key:"
    echo "  $PUBKEY"
    echo ""
    echo "To authorize additional Proxmox cluster nodes, run on each node:"
    echo "  ./install/host-setup.sh --node-index 2 --ssh-key \"$PUBKEY\""
    echo ""
  fi
  echo "Next steps (run from host via 'wheatley <cmd>' or inside CT via 'pct enter $CTID'):"
  echo "  1. Edit /etc/wheatley/secrets.env and /etc/wheatley/config.yaml"
  echo "  2. Auto-discover checks: wheatley discover --output /etc/wheatley/checks.yaml"
  echo "     (or review first: wheatley discover --output /etc/wheatley/checks.discovered.yaml)"
  echo "  3. Validate & start:     wheatley config validate && systemctl start wheatley"
  echo "================================================="
fi
