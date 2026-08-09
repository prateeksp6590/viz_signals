#!/usr/bin/env bash
# Switch the strategy configuration. Only STRATEGY keys are touched — credentials,
# URLs and instrument lists are never rewritten, so a rollback cannot break the
# connection to InfluxDB or Upstox.
#
#   bash deploy/apply_profile.sh B-itm-filtered
#   bash deploy/apply_profile.sh --list
#   bash deploy/apply_profile.sh --current
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=deploy/profiles
ENV=.env

if [ "${1:-}" = "--list" ] || [ $# -eq 0 ]; then
    echo "profiles:"; for f in "$DIR"/*.env; do
        echo "  $(basename "$f" .env)"
        sed -n '2,4p' "$f" | sed 's/^# /      /;s/^#$//'
    done; exit 0
fi
if [ "$1" = "--current" ]; then
    echo "active strategy keys in $ENV:"
    grep -hE '^(ANALYZE_SEGMENTS|ANALYZE_MONEYNESS|SHORT_SEGMENTS|EXIT_|ANGLE_Q|MAX_OPEN_POSITIONS|EQUITY_|MAX_NOTIONAL_PER_TRADE|MIN_PREMIUM|STRATEGY_NAME|ORDER_MODE|OHLC_SLOPE_|ANALYZE_EXCLUDE_SYMBOLS)' "$ENV" | sed 's/^/  /'
    exit 0
fi

P="$DIR/$1.env"
[ -f "$P" ] || { echo "no such profile: $1"; bash "$0" --list; exit 1; }

BAK="$ENV.$(date +%Y%m%d-%H%M%S).bak"
cp "$ENV" "$BAK"

python3 - "$ENV" "$P" <<'PY'
import re, sys
env_path, prof_path = sys.argv[1], sys.argv[2]
prof = {}
for line in open(prof_path):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        prof[k.strip()] = v.strip()
lines = open(env_path).read().splitlines()
out, seen = [], set()
for l in lines:
    m = re.match(r'^([A-Z_][A-Z0-9_]*)=', l)
    if m and m.group(1) in prof:
        k = m.group(1)
        if k in seen:            # drop duplicates rather than leave a shadow
            continue
        seen.add(k)
        out.append(f'{k}={prof[k]}')
    else:
        out.append(l)
for k, v in prof.items():
    if k not in seen:
        out.append(f'{k}={v}')
open(env_path, 'w').write('\n'.join(out) + '\n')
print(f'  applied {len(prof)} key(s)')
PY

echo "  backup: $BAK"
bash "$0" --current
echo
echo "restart to take effect:  sudo systemctl restart vizsignals vizapi"
