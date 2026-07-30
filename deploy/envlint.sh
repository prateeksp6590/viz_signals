#!/usr/bin/env bash
# Flag inline '# comments' on assignment lines: systemd EnvironmentFile= keeps them.
f="${1:-.env}"
bad=$(grep -nE "^[A-Z_][A-Z0-9_]*=.*[[:space:]]#" "$f" || true)
if [ -z "$bad" ]; then echo "OK  $f has no inline comments"; exit 0; fi
echo "WARN  $f has inline comments — systemd will pass them through verbatim:"
echo "$bad" | sed 's/^/   /'
echo
echo "fix in place (backs up to $f.bak):"
echo "  sed -i.bak -E 's/^([A-Z_][A-Z0-9_]*=[^[:space:]#]*)[[:space:]]+#.*$/\\1/' $f"
exit 1
