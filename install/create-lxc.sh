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

  echo "Wheatley LXC provisioning complete!"
  echo "CTID: $CTID is running at $IP"
fi
