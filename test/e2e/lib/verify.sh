#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/_common.sh"

ensure_run_dir

cleanup() {
  cleanup_stateful_resources
}

trap cleanup EXIT INT TERM

run_verify_playbook() {
  local -a argv
  argv=(
    "$DMF_ENV_REPO/bin/run-playbook.sh"
    "$ENV_ID"
    "$DMF_INFRA_REPO/k3s-lab-bootstrap/bootstrap-sandbox-verify.yml"
  )
  if [ -n "${E2E_SKIP_TAGS:-}" ]; then
    argv+=(--skip-tags "$E2E_SKIP_TAGS")
  fi
  local age_key_path="$DMF_DATA_ROOT/runs/$ENV_ID/age/keys.txt"
  [ -r "$age_key_path" ] || fail "verify: age key missing at $age_key_path"
  DMF_DATA_ROOT="$DMF_DATA_ROOT" SOPS_AGE_KEY_FILE="$age_key_path" "${argv[@]}"
}

monitoring_gate() {
  python3 - "$DMF_DATA_ROOT" "$ENV_ID" "$NEW_IP" "$LIMA_KEY" "$USER" <<'PY'
import json
import pathlib
import subprocess
import sys

data_root = pathlib.Path(sys.argv[1])
env_id = sys.argv[2]
new_ip = sys.argv[3]
ssh_key = sys.argv[4]
ssh_user = sys.argv[5]
keys = json.loads((data_root / "envs" / env_id / "openbao-keys.json").read_text(encoding="utf-8"))
ops_user = keys["ops_admin_username"]
ops_pass = keys["ops_admin_password"]

remote_script = r'''#!/usr/bin/env bash
set -euo pipefail

OPS_USER=$(cat <<'EOF'
__OPS_USER__
EOF
)
OPS_PASS=$(cat <<'EOF'
__OPS_PASS__
EOF
)

fetch_openbao_json() {
  sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n openbao exec -i openbao-0 -- sh <<EOF
set -euo pipefail
TOKEN=\$(BAO_ADDR=https://127.0.0.1:8200 BAO_SKIP_VERIFY=true \
  bao login -no-store -format=json -method=userpass \
  username='${OPS_USER}' password='${OPS_PASS}' 2>/dev/null \
  | sed -n 's/.*"client_token"[ ]*:[ ]*"\\([^"]*\\)".*/\\1/p')
[ -n "\$TOKEN" ] || exit 11
BAO_ADDR=https://127.0.0.1:8200 BAO_SKIP_VERIFY=true BAO_TOKEN="\$TOKEN" \
  bao kv get -format=json secret/apps/netbox/runtime
EOF
}

OPENBAO_JSON="$(fetch_openbao_json)"
PROMSD_TOKEN="$(printf '%s' "$OPENBAO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["data"].get("promsd_api_token",""))')"
[ -n "$PROMSD_TOKEN" ] || { echo '{"g1":false,"error":"promsd_api_token empty"}'; exit 0; }

ES_JSON="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring get externalsecret dmf-promsd-netbox -o json)"
ES_SYNC="$(printf '%s' "$ES_JSON" | python3 -c 'import json,sys; data=json.load(sys.stdin); conds=data.get("status",{}).get("conditions",[]); ok=any(c.get("type")=="Ready" and c.get("status")=="True" for c in conds); print("true" if ok else "false")')"
SECRET_JSON="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring get secret dmf-promsd-netbox -o json)"
SECRET_LEN="$(printf '%s' "$SECRET_JSON" | python3 -c 'import base64,json,sys; data=json.load(sys.stdin); raw=data.get("data",{}).get("netboxToken",""); print(len(base64.b64decode(raw)) if raw else 0)')"

POD_JSON="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring get pod -l app=dmf-promsd -o json)"
POD_NAME="$(printf '%s' "$POD_JSON" | python3 -c 'import json,sys; items=json.load(sys.stdin).get("items",[]); print(items[0]["metadata"]["name"] if items else "")')"
POD_PHASE="$(printf '%s' "$POD_JSON" | python3 -c 'import json,sys; items=json.load(sys.stdin).get("items",[]); print(items[0]["status"]["phase"] if items else "")')"
PROMSD_TOKEN_LEN="${#PROMSD_TOKEN}"
ANON_CODE="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring exec deploy/prometheus-server -- sh -lc "wget -S -O- http://netbox.netbox.svc.cluster.local/api/ipam/services/?tag=monitoring-probe\\&limit=1 2>&1 >/dev/null | sed -n 's/  HTTP\\/.* \\([0-9][0-9][0-9]\\).*/\\1/p' | tail -n 1")"
TOKEN_JSON="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring exec deploy/prometheus-server -- sh -lc "wget -qO- --header='Authorization: Bearer $PROMSD_TOKEN' http://netbox.netbox.svc.cluster.local/api/ipam/services/?tag=monitoring-probe\\&limit=0")"
TOKEN_COUNT="$(printf '%s' "$TOKEN_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("count",0))')"
SD_JSON="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring exec deploy/prometheus-server -- sh -lc "wget -qO- http://dmf-promsd.monitoring.svc.cluster.local:8000/sd/probe")"
SD_GROUPS="$(printf '%s' "$SD_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
PROM_JSON="$(sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml -n monitoring exec deploy/prometheus-server -- sh -lc "wget -qO- 'http://127.0.0.1:9090/prometheus/api/v1/query?query=up%7Bjob%3D%22netbox-probe%22%7D'")"
printf '%s\n' "$PROM_JSON" > /tmp/prom-query.json
PROM_TOTAL_PROM_UP="$(python3 -c "import json; data=json.load(open('/tmp/prom-query.json', encoding='utf-8')); results=data.get('data', {}).get('result', []); up=sum(1 for item in results if item.get('value', [None, '0'])[1] == '1'); print(len(results), up)")"
rm -f /tmp/prom-query.json
set -- $PROM_TOTAL_PROM_UP
PROM_TOTAL="$1"
PROM_UP="$2"

python3 - <<PY2
import json
payload = {
    "g1": True,
    "g2": "${ES_SYNC}" == "true" and int(${SECRET_LEN}) > 0,
    "g3": "${POD_PHASE}" == "Running" and "${ANON_CODE}" == "403" and int(${TOKEN_COUNT}) > 0,
    "g5": int(${TOKEN_COUNT}) > 0 and int(${SD_GROUPS}) == int(${TOKEN_COUNT}) and int(${PROM_TOTAL}) == int(${TOKEN_COUNT}) and int(${PROM_UP}) == int(${TOKEN_COUNT}),
    "promsd_token_length": int(${PROMSD_TOKEN_LEN}),
    "eso_synced": "${ES_SYNC}" == "true",
    "eso_secret_length": int(${SECRET_LEN}),
    "promsd_pod": "${POD_NAME}",
    "promsd_phase": "${POD_PHASE}",
    "anon_netbox_code": "${ANON_CODE}",
    "expected_probe_count": int(${TOKEN_COUNT}),
    "sd_probe_groups": int(${SD_GROUPS}),
    "prom_targets": int(${PROM_TOTAL}),
    "prom_up": int(${PROM_UP}),
}
print(json.dumps(payload))
PY2
'''

remote_script = remote_script.replace("__OPS_USER__", ops_user).replace("__OPS_PASS__", ops_pass)
result = subprocess.run(
    [
        "ssh",
        "-o",
        "LogLevel=ERROR",
        "-i",
        ssh_key,
        f"{ssh_user}@{new_ip}",
        "bash",
        "-s",
    ],
    input=remote_script,
    text=True,
    capture_output=True,
    check=False,
)
if result.returncode != 0:
    print(f"FAIL G1-G5 ssh gate execution failed: {result.stderr.strip() or result.stdout.strip()}")
    raise SystemExit(1)

payload = json.loads(result.stdout.strip().splitlines()[-1])
failed = []
for gate in ("g1", "g2", "g3", "g5"):
    if payload.get(gate):
        print(f"PASS {gate.upper()} {json.dumps(payload, sort_keys=True)}")
    else:
        print(f"FAIL {gate.upper()} {json.dumps(payload, sort_keys=True)}")
        failed.append(gate)
if failed:
    raise SystemExit(1)
PY
}

main() {
  ensure_env_context
  log "verify: running bootstrap-sandbox-verify.yml for $ENV_ID"
  run_verify_playbook
  log "verify: running monitoring SSH gate"
  monitoring_gate
  log "verify: complete"
}

main "$@"
