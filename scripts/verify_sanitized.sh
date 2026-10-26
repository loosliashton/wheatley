#!/usr/bin/env bash
set -euo pipefail

echo "=== Running Wheatley Sanitization & Secret Scanner ==="

FAILURES=0

# Patterns that should NOT be in tracked files (excluding examples, docs, and tests if specifically mocking)
# Check for private IP ranges in wheatley/ source code (outside config defaults)
if git grep -E "\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b" -- 'wheatley/' ':!*.example*' ':!*.md'; then
    echo "❌ ERROR: Hardcoded 192.168.x.x IP address found in source code!"
    FAILURES=$((FAILURES + 1))
fi

if git grep -E "\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b" -- 'wheatley/' ':!*.example*' ':!*.md'; then
    echo "❌ ERROR: Hardcoded 10.x.x.x IP address found in source code!"
    FAILURES=$((FAILURES + 1))
fi

# Check for hardcoded API keys or bot tokens (like bot<token> or AIzaSy...)
if git grep -E "(AIzaSy[0-9A-Za-z_-]{33}|[0-9]{9,10}:[A-Za-z0-9_-]{35})" -- ':!*.example*' ':!tests/*'; then
    echo "❌ ERROR: Potential live API key or Telegram bot token found!"
    FAILURES=$((FAILURES + 1))
fi

if [ $FAILURES -eq 0 ]; then
    echo "✅ Sanitization check passed: No private IPs or secrets detected in tracked source code."
    exit 0
else
    echo "❌ Sanitization check failed with $FAILURES issues."
    exit 1
fi
