#!/usr/bin/env bash
# ==============================================================================
# Wheatley Homelab Agent - Proxmox Host Setup Script
# ==============================================================================
# Run this script directly on any Proxmox VE node (e.g. cave, doug) to configure:
#   1. Dedicated API user (wheatley@pve)
#   2. Monitoring API token with PVEAuditor role
#   3. Prints the exact lines to add to your secrets.env file
# ==============================================================================

set -euo pipefail

if ! command -v pveum >/dev/null 2>&1; then
  echo "Error: This script must be run directly on a Proxmox VE host." >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "Error: This script must be run as root." >&2
  exit 1
fi

NODE_NAME=$(hostname)
echo "================================================="
echo " Configuring Wheatley Agent access on: $NODE_NAME"
echo "================================================="

# 1. Create wheatley@pve user if not already present
if ! pveum user list | grep -q "wheatley@pve"; then
  echo "Creating Proxmox user 'wheatley@pve'..."
  pveum user add wheatley@pve --comment "Wheatley Homelab Agent"
else
  echo "User 'wheatley@pve' already exists."
fi

# 2. Create API token (default: bot)
TOKEN_NAME="${1:-bot}"
echo "Generating API token 'wheatley@pve!${TOKEN_NAME}'..."
TOKEN_OUTPUT=$(pveum user token add wheatley@pve "$TOKEN_NAME" --privsep 0 --output-format json 2>&1 || true)

# Extract secret value
SECRET_VALUE=""
if echo "$TOKEN_OUTPUT" | grep -q '"value"'; then
  SECRET_VALUE=$(echo "$TOKEN_OUTPUT" | grep -o '"value":"[^"]*"' | cut -d'"' -f4)
else
  # If token already existed, inform user
  echo "Note: If the token already exists, token secret cannot be re-read."
  echo "To regenerate: pveum user token delete wheatley@pve $TOKEN_NAME && $0"
fi

# 3. Grant PVEAuditor role at /
echo "Assigning PVEAuditor role at '/' to wheatley@pve..."
pveum acl modify / -user wheatley@pve -role PVEAuditor

# 4. Extract SSL SHA256 Fingerprint
FINGERPRINT=""
if [[ -f /etc/pve/local/pve-ssl.pem ]]; then
  FINGERPRINT=$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256 | cut -d= -f2)
fi

echo ""
echo "================================================="
echo " Setup complete for node: $NODE_NAME"
echo " Add these entries to /etc/wheatley/secrets.env:"
echo "================================================="
if [[ -n "$SECRET_VALUE" ]]; then
  echo "${NODE_NAME^^}_TOKEN_MONITOR=\"wheatley@pve!${TOKEN_NAME}=${SECRET_VALUE}\""
  echo "${NODE_NAME^^}_TOKEN_OPS=\"wheatley@pve!${TOKEN_NAME}=${SECRET_VALUE}\""
else
  echo "# Set ${NODE_NAME^^}_TOKEN_MONITOR=\"wheatley@pve!${TOKEN_NAME}=<SECRET>\" with your token secret"
fi
if [[ -n "$FINGERPRINT" ]]; then
  echo "${NODE_NAME^^}_TLS_FINGERPRINT=\"${FINGERPRINT}\""
fi
echo "================================================="
