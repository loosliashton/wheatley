"""Command Allowlist Validator for Wheatley

Ensures strict read-only execution safety:
1. Rejects shell injection, chaining (;, &&, ||), backgrounding (&), and subshells ($(), ``).
2. Rejects file redirection (>, >>, <, <<).
3. Restricts pipes (|) exclusively to safe, read-only filters (grep, jq, awk, head, tail, etc.).
4. Matches the primary command against an authorized glob-based allowlist.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shlex
from typing import List, Optional, Tuple

logger = logging.getLogger("wheatley.tools.allowlist")

DEFAULT_ALLOWLIST: List[str] = [
    "uptime*",
    "free*",
    "df*",
    "top -b -n *",
    "journalctl*",
    "systemctl status*",
    "systemctl is-active*",
    "systemctl is-failed*",
    "docker ps*",
    "docker logs*",
    "docker inspect*",
    "docker stats --no-stream*",
    "zpool status*",
    "zpool list*",
    "smartctl -H*",
    "smartctl -a*",
    "smartctl -i*",
    "sensors*",
    "pvesm status*",
    "pvesm list*",
    "pveversion*",
    "uname*",
    "cat /proc/cpuinfo",
    "cat /proc/meminfo",
    "cat /etc/os-release",
    "ip addr*",
    "ip -br addr*",
    "ip route*",
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
        # Additional sanity checks on filter commands
        if filter_cmd == "awk":
            # Disallow awk system() or print to files
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

    # Reconstruct the primary command without outer pipeline
    # We join tokens, removing outer quotes if they were added by the lexer
    primary_cmd_str = " ".join(primary_tokens)

    # Build active allowlist
    allowlist_patterns = custom_allowlist if custom_allowlist is not None else DEFAULT_ALLOWLIST

    for pattern in allowlist_patterns:
        pat = pattern.strip()
        # Direct glob match against primary reconstructed command
        if fnmatch.fnmatch(primary_cmd_str, pat):
            return True, f"Matched allowlist pattern: {pat}"
        # Also check with quotes stripped or normalised spacing
        normalized_str = re.sub(r"\s+", " ", primary_cmd_str).strip()
        if fnmatch.fnmatch(normalized_str, pat):
            return True, f"Matched allowlist pattern: {pat}"

    return False, f"Command '{primary_cmd_str}' is not in the read-only allowlist"
