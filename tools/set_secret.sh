#!/usr/bin/env bash
# 逐项填写第三方密钥。输入时屏幕不显示；直接回车 = 保留原值不改。
# 密钥文件：/data/cf-n8n-secrets/worker/secrets.env（仓库外，权限 600，不进 git）
set -euo pipefail
F=${SECRETS_FILE:-/data/cf-n8n-secrets/worker/secrets.env}
[ -f "$F" ] || { echo "找不到 $F"; exit 1; }

ask() {  # ask KEY "说明" [show]
  local key=$1 desc=$2 show=${3:-} cur val
  cur=$(grep -E "^${key}=" "$F" | head -1 | cut -d= -f2- || true)
  local state="未填"; [ -n "$cur" ] && state="已填(${#cur}位)"
  if [ -n "$show" ]; then
    read -r -p "$desc [$state，当前：${cur:-空}]: " val
  else
    read -r -s -p "$desc [$state]: " val; echo
  fi
  val=$(printf '%s' "$val" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  [ -z "$val" ] && return 0
  local tmp; tmp=$(mktemp)
  if grep -qE "^${key}=" "$F"; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' "$F" > "$tmp"
  else
    cat "$F" > "$tmp"; echo "${key}=${val}" >> "$tmp"
  fi
  cat "$tmp" > "$F"; rm -f "$tmp"
  echo "  ✓ ${key} 已保存（${#val} 位）"
}

echo "== 填写密钥（直接回车跳过）=="
ask DEEPSEEK_API_KEY  "1/5 DeepSeek API Key（platform.deepseek.com → API Keys）"
ask TDOCS_MCP_TOKEN   "2/5 腾讯文档 MCP Token（docs.qq.com/open/document/mcp/get-token/）"
ask MOWEN_API_KEY     "3/5 墨问 API Key（小程序 → 我的 → 开发者选项 → 我的 API Key）"
ask LIVEPAGE_TOKEN    "4/5 活页令牌 sk-space-…（space.24haowan.com/app/tokens）"
ask TDOCS_SPACE_NAME  "5/5 腾讯文档空间名" show
chmod 600 "$F"
echo
echo "== 当前状态 =="
for k in DEEPSEEK_API_KEY TDOCS_MCP_TOKEN MOWEN_API_KEY LIVEPAGE_TOKEN; do
  v=$(grep -E "^${k}=" "$F" | head -1 | cut -d= -f2- || true)
  printf "  %-18s %s\n" "$k" "$([ -n "$v" ] && echo "已填(${#v}位)" || echo 未填)"
done
echo "完成。改完不用重启，系统下次运行自动读取。"
