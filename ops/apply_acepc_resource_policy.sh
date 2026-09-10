#!/usr/bin/env bash
set -euo pipefail

# Persistent host policy for the always-on mini-PC.
# - Ollama's Docker CPU quota is applied separately through AWB_OLLAMA_CPUS.
# - GoldenBull + Sportage share one aggregate low-priority slice capped at the
#   remaining fraction. They can divide it dynamically instead of being locked
#   to 7.5% each, and neither project is restarted by this script.

EXPERT_FRACTION="${AWB_OLLAMA_CPU_FRACTION:-0.85}"
SIDE_FRACTION="${AWB_SIDE_PROJECT_CPU_FRACTION:-0.15}"
CPU_COUNT="$(nproc)"

python3 - "$EXPERT_FRACTION" "$SIDE_FRACTION" <<'PY' >/tmp/awb-resource-policy-values
import sys
expert=float(sys.argv[1]); side=float(sys.argv[2])
if not (0.10 <= expert <= 0.95): raise SystemExit('AWB_OLLAMA_CPU_FRACTION must be in [0.10, 0.95]')
if not (0.01 <= side <= 0.50): raise SystemExit('AWB_SIDE_PROJECT_CPU_FRACTION must be in [0.01, 0.50]')
if expert + side > 1.001: raise SystemExit('Expert + side-project CPU fractions cannot exceed 1.0')
print(expert); print(side)
PY
mapfile -t FRACTIONS </tmp/awb-resource-policy-values
rm -f /tmp/awb-resource-policy-values
EXPERT_FRACTION="${FRACTIONS[0]}"
SIDE_FRACTION="${FRACTIONS[1]}"

OLLAMA_CPUS="$(python3 - "$CPU_COUNT" "$EXPERT_FRACTION" <<'PY'
import sys
n=int(sys.argv[1]); f=float(sys.argv[2]); print(f'{max(0.5,n*f):.3f}'.rstrip('0').rstrip('.'))
PY
)"
SIDE_QUOTA="$(python3 - "$CPU_COUNT" "$SIDE_FRACTION" <<'PY'
import sys
n=int(sys.argv[1]); f=float(sys.argv[2]); print(f'{max(1.0,n*f*100):.1f}%')
PY
)"

USER_UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$USER_UNITS/goldenbull.service.d" "$USER_UNITS/sportage.service.d"
cat >"$USER_UNITS/side-projects.slice" <<EOF
[Unit]
Description=GoldenBull and Sportage shared CPU envelope

[Slice]
CPUQuota=$SIDE_QUOTA
CPUWeight=100
EOF
for unit in goldenbull.service sportage.service; do
  mkdir -p "$USER_UNITS/${unit}.d"
  cat >"$USER_UNITS/${unit}.d/20-acepc-resource-budget.conf" <<'EOF'
[Service]
Slice=side-projects.slice
CPUWeight=100
EOF
done

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
systemctl --user daemon-reload
systemctl --user start side-projects.slice || true
# Apply the slice quota live where possible. Running services are deliberately
# not restarted; their persistent Slice= assignment takes effect at their next
# ordinary restart/deploy.
systemctl --user set-property --runtime side-projects.slice "CPUQuota=$SIDE_QUOTA" CPUWeight=100 || true

printf 'CPU_COUNT=%s\nAWB_OLLAMA_CPUS=%s\nSIDE_PROJECTS_QUOTA=%s\n' "$CPU_COUNT" "$OLLAMA_CPUS" "$SIDE_QUOTA"
printf 'GoldenBull and Sportage are configured to share side-projects.slice on their next normal service restart.\n'
