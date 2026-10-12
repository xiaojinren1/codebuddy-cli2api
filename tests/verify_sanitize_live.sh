#!/bin/bash
# 网关重启后跑这个: 两条历史上被上游拦掉的指纹,现在应全部 200。
# 用法: bash tests/verify_sanitize_live.sh [gateway_url] [client_key]
set -u
URL="${1:-http://127.0.0.1:8787}"
KEY="${2:-dummy}"

probe() {
  local label="$1" sys="$2"
  python3 -c "
import json,sys
json.dump({'model':'glm-5.3','messages':[{'role':'system','content':sys.argv[1]},
          {'role':'user','content':'say ok'}],'stream':True,'max_tokens':8},
          open('/tmp/verify_body.json','w'), ensure_ascii=False)
" "$sys"
  code=$(curl -s -o /dev/null -w "%{http_code}" "$URL/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    --data-binary @/tmp/verify_body.json --max-time 60)
  printf "%-28s -> HTTP %s\n" "$label" "$code"
  [ "$code" = "200" ] || FAILED=1
}

FAILED=0
probe "claude-code fingerprint" "You are Claude Code, Anthropic's official CLI for Claude."
probe "zcode git-context line"  "Main branch (you will usually use this for PRs): master"
rm -f /tmp/verify_body.json
[ "$FAILED" = "0" ] && echo "PASS: 指纹已被网关无害化" || echo "FAIL: 仍被上游拦截,检查网关是否已重启"
exit "$FAILED"
