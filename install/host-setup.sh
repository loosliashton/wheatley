#!/usr/bin/env bash
# ==============================================================================
# Wheatley Homelab Agent - Proxmox Host Setup Script
# ==============================================================================
# Run this script directly on any Proxmox VE node (e.g. cave, doug) to configure:
#   1. Dedicated API user (wheatley@pve) + PVEAuditor role
#   2. Monitoring API token
#   3. Restricted Linux system user (wheatley) with wheatley-shell
#   4. Narrow sudoers permissions for host and guest diagnostics
#   5. Authorized SSH key for CT 110
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_NAME="bot"
SSH_KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token-name|-t)
      TOKEN_NAME="$2"
      shift 2
      ;;
    --ssh-key|-k)
      SSH_KEY="$2"
      shift 2
      ;;
    --help|-h)
      echo "Usage: $0 [--token-name bot] [--ssh-key \"ssh-ed25519 ...\"]"
      exit 0
      ;;
    *)
      # Support legacy positional argument for token name
      if [[ -z "${TOKEN_NAME_SET:-}" ]]; then
        TOKEN_NAME="$1"
        TOKEN_NAME_SET=1
        shift
      else
        echo "Unknown argument: $1" >&2
        exit 1
      fi
      ;;
  esac
done

NODE_NAME=$(hostname)
echo "================================================="
echo " Configuring Wheatley Agent access on: $NODE_NAME"
echo "================================================="

# 1. Create wheatley@pve user if not already present
if ! pveum user list | grep -q "wheatley@pve"; then
  echo "[1/5] Creating Proxmox user 'wheatley@pve'..."
  pveum user add wheatley@pve --comment "Wheatley Homelab Agent"
else
  echo "[1/5] User 'wheatley@pve' already exists."
fi

# 2. Create API token
echo "[2/5] Generating API token 'wheatley@pve!${TOKEN_NAME}'..."
TOKEN_OUTPUT=$(pveum user token add wheatley@pve "$TOKEN_NAME" --privsep 0 --output-format json 2>&1 || true)

SECRET_VALUE=""
if echo "$TOKEN_OUTPUT" | grep -q '"value"'; then
  SECRET_VALUE=$(echo "$TOKEN_OUTPUT" | grep -o '"value":"[^"]*"' | cut -d'"' -f4)
else
  echo "Note: Token already exists or secret was not returned."
  echo "To regenerate: pveum user token delete wheatley@pve $TOKEN_NAME && $0"
fi

# 3. Grant PVEAuditor role at /
echo "[3/5] Assigning PVEAuditor role at '/' to wheatley@pve..."
pveum acl modify / -user wheatley@pve -role PVEAuditor

# 4. Install restricted wheatley-shell and create system user
echo "[4/5] Setting up restricted system user 'wheatley'..."
RESTRICTED_SHELL="/usr/local/bin/wheatley-shell"
if [[ -f "${SCRIPT_DIR}/wheatley-shell" ]]; then
  install -m 755 "${SCRIPT_DIR}/wheatley-shell" "$RESTRICTED_SHELL"
else
  cat <<'EOF' > "$RESTRICTED_SHELL"
#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  echo "Wheatley shell: interactive shell sessions are disabled." >&2
  exit 1
fi
exec /bin/bash -c "$SSH_ORIGINAL_COMMAND"
EOF
  chmod 755 "$RESTRICTED_SHELL"
fi

if ! grep -q "^${RESTRICTED_SHELL}$" /etc/shells 2>/dev/null; then
  echo "$RESTRICTED_SHELL" >> /etc/shells
fi

if ! id wheatley >/dev/null 2>&1; then
  useradd -m -s "$RESTRICTED_SHELL" -c "Wheatley Homelab Agent" wheatley
  echo "Created user 'wheatley' with shell '$RESTRICTED_SHELL'."
else
  usermod -s "$RESTRICTED_SHELL" wheatley
  echo "Updated user 'wheatley' shell to '$RESTRICTED_SHELL'."
fi

# Set up SSH authorized keys if provided
if [[ -n "$SSH_KEY" ]]; then
  mkdir -p /home/wheatley/.ssh
  chmod 700 /home/wheatley/.ssh
  if ! grep -Fxq "$SSH_KEY" /home/wheatley/.ssh/authorized_keys 2>/dev/null; then
    echo "$SSH_KEY" >> /home/wheatley/.ssh/authorized_keys
  fi
  chmod 600 /home/wheatley/.ssh/authorized_keys
  chown -R wheatley:wheatley /home/wheatley/.ssh
  echo "Installed SSH authorized key for 'wheatley'."
fi

# 5. Configure narrow sudoers rules
echo "[5/5] Configuring narrow sudoers permissions..."
SUDOERS_FILE="/etc/sudoers.d/wheatley"
cat <<'EOF' > "$SUDOERS_FILE"
# Wheatley Homelab Agent - Narrow Sudoers Permissions
wheatley ALL=(ALL) NOPASSWD: \
    /usr/sbin/pct exec *, \
    /usr/sbin/qm guest exec *, \
    /usr/sbin/pvesm status*, \
    /usr/sbin/zpool status*, \
    /usr/sbin/zpool list*, \
    /usr/sbin/smartctl *, \
    /usr/bin/sensors *, \
    /bin/systemctl status *, \
    /usr/bin/systemctl status *, \
    /bin/journalctl *, \
    /usr/bin/journalctl *, \
    /bin/df *, \
    /usr/bin/df *, \
    /usr/bin/uptime, \
    /usr/bin/free *
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE"

# Extract SSL SHA256 Fingerprint
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
echo " Host setup successfully configured."
echo "================================================="
