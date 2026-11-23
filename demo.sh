#!/usr/bin/env bash
# 30-second demo: three fake agents want to edit the same file and run a heavy build on a machine that
# can afford one build at a time. A fourth agent crashes while holding a claim.
# Runs in a throwaway directory; touches nothing else.  Usage: ./demo.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
AK="python3 $HERE/bin/agentlanes"
DEMO=$(mktemp -d "${TMPDIR:-/tmp}/agentlanes-demo.XXXXXX")
export AGENTLANES_STATE_DIR=$DEMO/state
W=$DEMO/repo; mkdir -p "$W"; echo "# app" > "$W/app.py"; echo "debug: false" > "$W/config.yaml"
T0=$(date +%s)
say() { printf '%3ss  %-8s %s\n' $(( $(date +%s) - T0 )) "$AGENTLANES_AGENT" "${*//$W\//}"; }

agent() {   # $1 name  $2 change
  export AGENTLANES_AGENT=$1
  local me; me=${BASHPID:-$(sh -c 'echo $PPID')}   # bash 3.2 (macOS) has no BASHPID
  say "wants to edit app.py"
  until out=$($AK claim "$1-edit" --paths "$W/app.py" --pid "$me" --desc "$2"); do
    say "BLOCKED: ${out%% -- use*}"
    $AK events wait --kind claim.done --timeout 3 >/dev/null
  done
  say "claimed app.py, editing"
  echo "$1: $2" >> "$W/app.py"; sleep 2
  $AK release "$1-edit" >/dev/null; say "released app.py"
  say "queued for the build slot (1 slot)"
  $AK slot build 1 -- bash -c "sleep 3"
  say "build finished"
}

crasher() {
  export AGENTLANES_AGENT=agent-D
  local me; me=${BASHPID:-$(sh -c 'echo $PPID')}   # bash 3.2 (macOS) has no BASHPID
  $AK claim "D-config" --paths "$W/config.yaml" --pid "$me" --desc "turn on debug" >/dev/null
  say "claimed config.yaml ... and crashes (kill -9)"
  kill -9 "$me"
}

echo "== 3 agents, 1 file, 1 build slot; agent-D crashes holding config.yaml"
{ ( crasher ); } 2>/dev/null
agent agent-A "add login route" &
sleep 0.3; agent agent-B "fix typo in header" &
sleep 0.3; agent agent-C "rename helper" &
sleep 0.6
echo "--- status while they work:"
$AK status | sed -n '/== claims/,/== jobs/p' | grep -v '== jobs' | sed "s#$W/##g"
wait
export AGENTLANES_AGENT=agent-A
say "wants config.yaml (agent-D died holding it)"
$AK claim A-config --paths "$W/config.yaml" --pid $$ >/dev/null && say "claimed config.yaml: agent-D's lease ended with its process"
$AK release A-config >/dev/null
echo "--- app.py (one edit at a time, nothing lost):"
cat "$W/app.py"
echo "--- event log:"
$AK events tail -n 100 | grep -E 'claim\.(conflict|reap)|slot\.(acquire|release)' | sed "s#$W/##g"
echo "--- summary:"
$AK events stats | head -12
if $AK run --dry-run --write "$W" -- true >/dev/null 2>&1; then   # Linux with Landlock: show the kernel lane
  echo "--- kernel lane (Landlock): agent-A runs a shell under 'agentlanes run', holding a claim on src/ only"
  mkdir -p "$W/src" "$W/conf"; echo "x = 1" > "$W/src/app.py"; echo "debug: false" > "$W/conf/config.yaml"
  export AGENTLANES_AGENT=agent-A
  $AK claim A-src --paths "$W/src" >/dev/null
  $AK run -q -- sh -c "echo 'y = 2' >> '$W/src/app.py' && echo '  inside lane:  appended to src/app.py'
    sed -i s/false/true/ '$W/conf/config.yaml' 2>&1 | sed 's#$W/##; s/^/  outside lane: /'
    cp /etc/hostname '$W/conf/stolen' 2>&1 | sed 's#$W/##; s/^/  outside lane: /'"
  echo "  conf/config.yaml still says: $(cat "$W/conf/config.yaml")"
  $AK release A-src >/dev/null
fi
rm -rf "$DEMO"
