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

# Auto-detect template if not provided
if [[ -z "$TEMPLATE" ]]; then
  echo "Detecting available Debian template on storage..."
  # Prioritize Debian 13, then Debian 12
  for tmpl in $(pveam list local 2>/dev/null | awk '{print $1}' | grep -E 'debian-(13|12)-standard' | sort -rV); do
    TEMPLATE="$tmpl"
    break
  done

  if [[ -z "$TEMPLATE" ]]; then
    echo "No local Debian 12 or 13 template found. Checking downloadable templates..."
    pveam update >/dev/null 2>&1 || true
    AVAILABLE_TMPL=$(pveam available | awk '{print $2}' | grep -E 'debian-(13|12)-standard' | sort -rV | head -n1)
    if [[ -n "$AVAILABLE_TMPL" ]]; then
      echo "Downloading template $AVAILABLE_TMPL to local..."
      pveam download local "$AVAILABLE_TMPL"
      TEMPLATE="local:vztmpl/$AVAILABLE_TMPL"
    else
      echo "Error: Could not find or download a Debian template." >&2
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
    ln -sf /opt/wheatley/venv/bin/wheatley /usr/local/bin/wheatley

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

  # Authorize SSH key on local Proxmox host if wheatley user exists
  if [[ -n "$PUBKEY" ]] && id -u wheatley >/dev/null 2>&1; then
    mkdir -p /home/wheatley/.ssh
    chmod 700 /home/wheatley/.ssh
    if ! grep -Fxq "$PUBKEY" /home/wheatley/.ssh/authorized_keys 2>/dev/null; then
      echo "$PUBKEY" >> /home/wheatley/.ssh/authorized_keys
    fi
    chmod 600 /home/wheatley/.ssh/authorized_keys
    chown -R wheatley:wheatley /home/wheatley/.ssh
    echo "Authorized CT $CTID SSH key for user 'wheatley' on local Proxmox host."
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
  echo "Next steps (run 'pct enter $CTID'):"
  echo "  1. Edit /etc/wheatley/secrets.env and /etc/wheatley/config.yaml"
  echo "  2. Auto-discover checks: wheatley discover --output /etc/wheatley/checks.yaml"
  echo "  3. Validate & start:     wheatley config validate && systemctl start wheatley"
  echo "================================================="
fi
