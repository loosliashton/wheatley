#!/usr/bin/env bash
# ==============================================================================
# Wheatley Homelab Agent - Proxmox Host Setup Script
# ==============================================================================
# Run this script directly on any Proxmox VE node (e.g. pve-node-01, pve-node-02) to configure:
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

# Ensure sudo/visudo is installed (Proxmox VE minimal installs do not include sudo by default)
if ! command -v visudo >/dev/null 2>&1; then
  echo "Prerequisite 'sudo' not found. Installing via apt-get..."
  apt-get update -y && apt-get install -y --no-install-recommends sudo
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_NAME="bot"
SSH_KEY=""
NODE_INDEX="1"

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
    --node-index|-n)
      NODE_INDEX="$2"
      shift 2
      ;;
    --help|-h)
      echo "Usage: $0 [--token-name bot] [--node-index 1] [--ssh-key \"ssh-ed25519 ...\"]"
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
  echo "API token secret generated. It is printed once in the summary below; save it now (Proxmox cannot display it again)."
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
  # Fallback for standalone use of this script. MUST stay byte-identical to
  # install/wheatley-shell.
  cat <<'EOF' > "$RESTRICTED_SHELL"
#!/usr/bin/env bash
# ==============================================================================
# Wheatley Non-Interactive SSH Shell Wrapper
# Installed on Proxmox VE hosts as the login shell of the 'wheatley' SSH user.
#
# What this wrapper enforces:
# - Interactive login sessions are disabled (a command must be supplied).
# - Multiline commands are rejected.
# - Guest exec commands must match the exact form Wheatley generates:
#     sudo /usr/sbin/pct exec <vmid> -- /bin/bash -c <one shell word>
#     sudo /usr/sbin/qm guest exec <vmid> -- /bin/bash -c <one shell word>
#   where <one shell word> is a shlex.quote()-style argument. Nothing may follow it.
# - Direct host commands may use pipes and redirections, but command chaining
#   (';', '&&', '||', background '&'), command/process substitution ('$(...)',
#   backticks, '<(...)', '>(...)') and ANSI-C quoting ($'...') are rejected.
#   Quoting is taken into account, so e.g. awk '$3 > 0' is permitted.
#
# What this wrapper does NOT do:
# - It is not a privilege boundary and does not restrict which programs the
#   (unprivileged) 'wheatley' user can run. Root-level capabilities on the host
#   are governed exclusively by /etc/sudoers.d/wheatley, and which commands
#   Wheatley may run without approval is governed by its application allowlist.
# ==============================================================================
set -euo pipefail

CMD=""
if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  CMD="$SSH_ORIGINAL_COMMAND"
elif [[ $# -ge 2 && "$1" == "-c" ]]; then
  CMD="$2"
fi

deny() {
  echo "Wheatley shell: $1" >&2
  exit 1
}

if [[ -z "$CMD" ]]; then
  deny "interactive shell sessions are disabled."
fi

# Reject multiline commands and NUL-like control characters
if [[ "$CMD" == *$'\n'* || "$CMD" == *$'\r'* ]]; then
  deny "multiline commands are prohibited."
fi

# --- Guest exec form ----------------------------------------------------------
# A single shell word as produced by Python's shlex.quote(): either a run of
# "safe" characters, or a concatenation of '...' segments and "'" escapes.
SAFE_WORD="[A-Za-z0-9_@%+=:,./-]+"
QUOTED_WORD="('[^']*'|\"'\")+"
GUEST_PREFIX="^sudo[[:space:]]+/usr/sbin/(pct[[:space:]]+exec|qm[[:space:]]+guest[[:space:]]+exec)[[:space:]]+"
GUEST_STRICT="${GUEST_PREFIX}[0-9]+[[:space:]]+--[[:space:]]+/bin/bash[[:space:]]+-c[[:space:]]+(${SAFE_WORD}|${QUOTED_WORD})[[:space:]]*$"

if [[ "$CMD" =~ $GUEST_STRICT ]]; then
  # The quoted payload runs inside the guest; it is a single argument to bash -c
  # in the guest and cannot affect the host shell.
  exec /bin/bash -c "$CMD"
fi

if [[ "$CMD" =~ $GUEST_PREFIX ]]; then
  deny "malformed guest exec command (expected: sudo /usr/sbin/{pct exec|qm guest exec} <vmid> -- /bin/bash -c <single quoted argument>)."
fi

# --- Direct host commands -------------------------------------------------------
# Defense-in-depth only: sudoers does not permit these anyway.
if [[ "$CMD" =~ (^|[[:space:]|])(sudo[[:space:]]+)?(/usr/sbin/)?(pct|qm)[[:space:]]+destroy([[:space:]]|$) ]]; then
  deny "guest destruction (pct/qm destroy) is prohibited."
fi
if [[ "$CMD" =~ (^|[[:space:]|])sudo[[:space:]]+(-i|-s|su|bash|sh|/bin/bash|/bin/sh)([[:space:]]|$) ]]; then
  deny "interactive root shell escalation is prohibited."
fi

# Quote-aware scan for chaining and substitution outside of single quotes.
# Returns 0 if the command is acceptable, 1 otherwise.
check_host_syntax() {
  local s="$1" len=${#1} i c next prev="" sq=0 dq=0
  for ((i = 0; i < len; i++)); do
    c="${s:i:1}"
    next="${s:i+1:1}"
    if ((sq)); then
      [[ "$c" == "'" ]] && sq=0
      prev="$c"
      continue
    fi
    if [[ "$c" == "\\" ]]; then
      # Skip the escaped character (both unquoted and inside double quotes)
      i=$((i + 1))
      prev=""
      continue
    fi
    if ((dq)); then
      case "$c" in
        '"') dq=0 ;;
        '`') return 1 ;;
        '$') [[ "$next" == "(" ]] && return 1 ;;
      esac
      prev="$c"
      continue
    fi
    case "$c" in
      "'") sq=1 ;;
      '"') dq=1 ;;
      ';' | '`') return 1 ;;
      '$') [[ "$next" == "(" || "$next" == "'" ]] && return 1 ;;
      '|') [[ "$next" == "|" ]] && return 1 ;;
      '<' | '>') [[ "$next" == "(" ]] && return 1 ;;
      '&')
        [[ "$next" == "&" ]] && return 1
        # Permit redirection forms only: >&N, <&N, &>file
        if [[ "$prev" != ">" && "$prev" != "<" && "$next" != ">" ]]; then
          return 1
        fi
        ;;
    esac
    prev="$c"
  done
  ((sq || dq)) && return 1
  return 0
}

if ! check_host_syntax "$CMD"; then
  deny "command chaining (; && || &), command/process substitution, ANSI-C quoting, and unbalanced quotes are prohibited on host."
fi

# Execute the validated non-interactive command
exec /bin/bash -c "$CMD"
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

# Auto-detect SSH public key from local CT 110 if not explicitly passed
if [[ -z "$SSH_KEY" ]] && command -v pct >/dev/null 2>&1 && pct status 110 >/dev/null 2>&1; then
  SSH_KEY=$(pct exec 110 -- cat /var/lib/wheatley/.ssh/id_ed25519.pub 2>/dev/null || true)
fi

# Set up SSH authorized keys if provided (with forwarding and PTY disabled).
# Any existing entry for the same key (e.g. an unrestricted line from an older
# install) is replaced so upgrades also receive the restrictions.
install_restricted_key() {
  local key="$1"
  local ak="/home/wheatley/.ssh/authorized_keys"
  local opts="no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"
  local key_part blob tmp
  key_part=$(printf '%s\n' "$key" | grep -oE '(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+( .*)?$' | head -n1 || true)
  if [[ -z "$key_part" ]]; then
    echo "Warning: could not parse SSH public key '${key}'; skipping authorized_keys setup." >&2
    return 1
  fi
  blob=$(printf '%s\n' "$key_part" | awk '{print $2}')
  mkdir -p /home/wheatley/.ssh
  chmod 700 /home/wheatley/.ssh
  touch "$ak"
  tmp=$(mktemp "${ak}.XXXXXX")
  grep -vF -- "$blob" "$ak" > "$tmp" || true
  printf '%s %s\n' "$opts" "$key_part" >> "$tmp"
  mv "$tmp" "$ak"
  chmod 600 "$ak"
  chown -R wheatley:wheatley /home/wheatley/.ssh
}

if [[ -n "$SSH_KEY" ]]; then
  if install_restricted_key "$SSH_KEY"; then
    echo "Installed SSH authorized key for 'wheatley' (port/agent/X11 forwarding and PTY disabled)."
  fi
fi

# 5. Configure narrow sudoers rules
echo "[5/5] Configuring narrow sudoers permissions..."
SUDOERS_FILE="/etc/sudoers.d/wheatley"
cat <<'EOF' > "$SUDOERS_FILE"
# Wheatley Homelab Agent - Narrow Sudoers Permissions
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
    /usr/bin/sensors, \
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
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE"

# Extract SSL SHA256 Fingerprint
FINGERPRINT=""
if [[ -f /etc/pve/local/pve-ssl.pem ]]; then
  FINGERPRINT=$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256 | cut -d= -f2)
fi

CT_EXISTS=false
if command -v pct >/dev/null 2>&1 && pct status 110 >/dev/null 2>&1; then
  CT_EXISTS=true
fi

echo ""
echo "================================================="
echo " Setup complete for node: $NODE_NAME (NODE${NODE_INDEX})"
if [[ "$NODE_INDEX" -gt 1 || -n "$SSH_KEY" ]]; then
  echo " Save these entries! Paste them into"
  echo " /etc/wheatley/secrets.env inside the Wheatley"
  echo " container (CT 110 on your primary Proxmox node):"
elif [[ "$CT_EXISTS" == "true" ]]; then
  echo " Save these entries! Paste them into"
  echo " /etc/wheatley/secrets.env inside the Wheatley"
  echo " container (CT 110):"
else
  echo " Save these entries! You will paste them into"
  echo " /etc/wheatley/secrets.env inside the Wheatley"
  echo " container (CT 110) after running create-lxc.sh:"
fi
echo "================================================="
if [[ -n "$SECRET_VALUE" ]]; then
  echo "NODE${NODE_INDEX}_TOKEN_MONITOR=\"wheatley@pve!${TOKEN_NAME}=${SECRET_VALUE}\""
else
  echo "# Set NODE${NODE_INDEX}_TOKEN_MONITOR=\"wheatley@pve!${TOKEN_NAME}=<SECRET>\" with your token secret"
fi
if [[ -n "$FINGERPRINT" ]]; then
  echo "NODE${NODE_INDEX}_TLS_FINGERPRINT=\"${FINGERPRINT}\""
fi
echo "================================================="
if [[ "$NODE_INDEX" -gt 1 || -n "$SSH_KEY" ]]; then
  echo " Next step: Add NODE${NODE_INDEX}_* to /etc/wheatley/secrets.env on CT 110."
elif [[ "$CT_EXISTS" == "true" ]]; then
  echo " Next step: Paste credentials into /etc/wheatley/secrets.env inside CT 110 (or run 'pct enter 110')."
else
  echo " Next step: Run ./install/create-lxc.sh to provision the Wheatley container."
fi
echo "================================================="
