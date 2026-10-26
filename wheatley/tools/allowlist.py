"""Command Allowlist Validator for Wheatley

Ensures strict read-only execution safety:
1. Rejects shell injection, chaining (;, &&, ||), backgrounding (&), and subshells ($(), ``).
2. Rejects file redirection (>, >>, <, <<).
3. Restricts pipes (|) exclusively to safe, read-only filters (grep, jq, awk, head, tail, etc.).
4. Checks for forbidden mutating flags/arguments on known diagnostic tools.
5. Matches the primary command against an authorized glob-based allowlist.
6. Validates proposed user-learned patterns before they can be added to the allowlist.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
import re
import shlex
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("wheatley.tools.allowlist")

DEFAULT_ALLOWLIST: List[str] = [
    "uptime",
    "free",
    "free -*",
    "df",
    "df -*",
    "top -b -n *",
    "journalctl",
    "journalctl -n *",
    "journalctl -u *",
    "journalctl --no-pager*",
    "systemctl status *",
    "systemctl is-active *",
    "systemctl is-failed *",
    "systemctl list-units*",
    "systemctl list-timers*",
    "docker ps",
    "docker ps -*",
    "docker logs *",
    "docker inspect *",
    "docker stats --no-stream*",
    "zpool status",
    "zpool status *",
    "zpool list",
    "zpool list *",
    "smartctl --scan",
    "smartctl -H *",
    "smartctl -a *",
    "smartctl -i *",
    "smartctl -x *",
    "sensors",
    "sensors -j*",
    "lsblk",
    "lsblk *",
    "pvesm status",
    "pvesm status *",
    "pvesm list *",
    "pveversion",
    "pveversion *",
    "uname*",
    "cat /proc/cpuinfo",
    "cat /proc/meminfo",
    "cat /etc/os-release",
    "ip addr",
    "ip addr show*",
    "ip -br addr",
    "ip -br addr show*",
    "ip link",
    "ip link show*",
    "ip -br link",
    "ip -br link show*",
    "ip route",
    "ip route show*",
]

ALLOWED_PIPELINE_FILTERS = {
    "grep",
    "egrep",
    "fgrep",
    "awk",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "cut",
    "jq",
    "tr",
    "column",
    "cat",
}

FORBIDDEN_OPERATORS = {
    ";",
    "&&",
    "||",
    "&",
    ">",
    ">>",
    "<",
    "<<",
    ">&",
    "<&",
    "|&",
}

FORBIDDEN_ARGUMENTS: Dict[str, Set[str]] = {
    "journalctl": {
        "--vacuum-size",
        "--vacuum-time",
        "--vacuum-files",
        "--rotate",
        "--flush",
        "--relinquish-var",
        "--smart-relinquish-var",
    },
    "ip": {"add", "del", "delete", "flush", "change", "replace", "set", "link-netnsid"},
    "smartctl": {"-s", "--smart=", "-o", "--offline=", "-T", "--tolerance="},
    "sensors": {"-s", "--set"},
    "systemctl": {
        "start",
        "stop",
        "restart",
        "reload",
        "kill",
        "mask",
        "unmask",
        "enable",
        "disable",
        "edit",
        "revert",
        "reset-failed",
    },
    "docker": {
        "run",
        "create",
        "rm",
        "kill",
        "stop",
        "restart",
        "pause",
        "unpause",
        "exec",
        "build",
        "pull",
        "push",
        "commit",
    },
    "zpool": {
        "create",
        "destroy",
        "add",
        "attach",
        "detach",
        "replace",
        "remove",
        "offline",
        "online",
        "clear",
        "reguid",
        "reopen",
        "set",
        "trim",
        "scrub",
    },
}

DISALLOWED_PATTERN_BINARIES = {
    "bash",
    "sh",
    "zsh",
    "dash",
    "python",
    "python3",
    "perl",
    "ruby",
    "rm",
    "dd",
    "mkfs",
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "socat",
    "ssh",
    "scp",
    "rsync",
    "chmod",
    "chown",
    "mv",
    "cp",
    "kill",
    "killall",
    "pkill",
    "sudo",
    "su",
}


def is_command_allowed(
    command: str, custom_allowlist: Optional[List[str]] = None
) -> Tuple[bool, str]:
    """Validate whether a shell command is safe and permitted.

    Args:
        command: The command line string to evaluate.
        custom_allowlist: Optional list of glob patterns overriding/extending defaults.

    Returns:
        (is_allowed: bool, reason: str)
    """
    cmd_str = command.strip()
    if not cmd_str:
        return False, "Empty command"

    # Reject literal newlines or null bytes
    if "\n" in cmd_str or "\r" in cmd_str or "\0" in cmd_str:
        return False, "Command contains newline or null characters"

    # Quick regex checks for subshells and backticks before tokenizing
    if "`" in cmd_str:
        return False, "Backtick subshells (`) are forbidden"
    if re.search(r"\$\(.*?\)|\$\{.*?\}", cmd_str):
        return False, "Command substitution $() and variable expansion ${} are forbidden"

    # Tokenize with punctuation recognition
    try:
        lexer = shlex.shlex(cmd_str, punctuation_chars=True)
        tokens = list(lexer)
    except Exception as e:
        return False, f"Failed to parse command syntax: {e}"

    if not tokens:
        return False, "Empty command tokens"

    # Check for forbidden operators
    for i, tok in enumerate(tokens):
        if tok in FORBIDDEN_OPERATORS:
            return False, f"Operator '{tok}' is forbidden"
        if tok == "$" and i + 1 < len(tokens) and tokens[i + 1] in ("(", "{"):
            return False, "Command substitution or variable expansion is forbidden"

    # Split into pipeline segments by '|'
    pipeline_segments: List[List[str]] = []
    current_segment: List[str] = []
    for tok in tokens:
        if tok == "|":
            if not current_segment:
                return False, "Invalid pipe syntax (empty stage)"
            pipeline_segments.append(current_segment)
            current_segment = []
        else:
            current_segment.append(tok)

    if not current_segment:
        return False, "Invalid trailing pipe syntax"
    pipeline_segments.append(current_segment)

    # Validate pipeline filter stages (stages 1+)
    for seg in pipeline_segments[1:]:
        filter_cmd = seg[0].strip("\"'")
        if filter_cmd not in ALLOWED_PIPELINE_FILTERS:
            return (
                False,
                f"Piping to '{filter_cmd}' is forbidden; only safe filters are permitted ({', '.join(sorted(ALLOWED_PIPELINE_FILTERS))})",
            )
        if filter_cmd == "awk":
            joined_awk = " ".join(seg)
            if "system(" in joined_awk or "getline" in joined_awk or ">" in joined_awk:
                return False, "Forbidden function or redirection in awk command"

    # Primary command is segment 0
    primary_tokens = pipeline_segments[0]
    # Strip optional leading 'sudo' if present for validation
    if primary_tokens[0].strip("\"'") == "sudo":
        primary_tokens = primary_tokens[1:]
        if not primary_tokens:
            return False, "Command cannot be only 'sudo'"

    base_cmd = Path(primary_tokens[0].strip("\"'")).name

    # Check for forbidden mutating arguments on known commands
    if base_cmd in FORBIDDEN_ARGUMENTS:
        forbidden_set = FORBIDDEN_ARGUMENTS[base_cmd]
        for tok in primary_tokens[1:]:
            cleaned_tok = tok.strip("\"'")
            for bad in forbidden_set:
                if cleaned_tok == bad or (bad.endswith("=") and cleaned_tok.startswith(bad)):
                    return False, f"Forbidden mutating argument '{cleaned_tok}' on command '{base_cmd}'"

    # Reconstruct the primary command without outer pipeline
    primary_cmd_str = " ".join(primary_tokens)

    # Build active allowlist
    allowlist_patterns = custom_allowlist if custom_allowlist is not None else DEFAULT_ALLOWLIST

    for pattern in allowlist_patterns:
        pat = pattern.strip()
        if fnmatch.fnmatch(primary_cmd_str, pat):
            return True, f"Matched allowlist pattern: {pat}"
        normalized_str = re.sub(r"\s+", " ", primary_cmd_str).strip()
        if fnmatch.fnmatch(normalized_str, pat):
            return True, f"Matched allowlist pattern: {pat}"
        if pat.endswith(" *") and (primary_cmd_str == pat[:-2] or normalized_str == pat[:-2]):
            return True, f"Matched allowlist pattern: {pat}"

    return False, f"Command '{primary_cmd_str}' is not in the read-only allowlist"


def validate_proposed_pattern(pattern: str) -> Tuple[bool, str]:
    """Validate that a proposed allowlist pattern meets strict safety criteria.

    Rules:
    1. Must not contain shell operators (; & | > < $ ` etc.).
    2. At most one wildcard '*' allowed, and if present it must be at the very end.
    3. Must have a fixed base command and subcommand (minimum 2 tokens before any '*').
       Example: 'snapraid status*' is VALID; 'snapraid*' is INVALID (too broad).
    4. Base binary must not be an interpreter, shell, or destructive command.
    """
    pat = pattern.strip()
    if not pat:
        return False, "Pattern cannot be empty"

    for op in (";", "&&", "||", "&", ">", "<", "`", "$", "|"):
        if op in pat:
            return False, f"Pattern contains forbidden shell operator '{op}'"

    star_count = pat.count("*")
    if star_count > 1:
        return False, "Pattern can contain at most one wildcard '*'"
    if star_count == 1 and not pat.endswith("*"):
        return False, "Wildcard '*' is only permitted at the very end of the pattern"

    # Tokenize pattern base (without trailing *)
    cleaned = pat[:-1].strip() if pat.endswith("*") else pat
    parts = cleaned.split()
    if not parts:
        return False, "Pattern has no command tokens"

    base_bin = Path(parts[0]).name.lower()
    if base_bin in DISALLOWED_PATTERN_BINARIES:
        return False, f"Command '{base_bin}' cannot be added to allowlist (interpreter/destructive binary)"

    # Reject forbidden mutating arguments for known commands
    if base_bin in FORBIDDEN_ARGUMENTS:
        for p in parts[1:]:
            cleaned_p = p.strip("\"'")
            for bad in FORBIDDEN_ARGUMENTS[base_bin]:
                if cleaned_p == bad or (bad.endswith("=") and cleaned_p.startswith(bad)):
                    return False, f"Pattern contains forbidden mutating argument '{cleaned_p}' for '{base_bin}'"

    # Require at least 2 tokens before wildcard for compound tools (unless single tool is exact match)
    if star_count > 0 and len(parts) < 2:
        return False, f"Pattern '{pattern}' is too broad; must include a specific subcommand (e.g. '{base_bin} status*')"

    return True, "Valid pattern"
