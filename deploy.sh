#!/usr/bin/env bash
# 一键部署 / 更新（可重复跑）：
#   生成系统密钥 → 起容器 → 建 n8n 管理员 → 导入并发布工作流 → 初始化腾讯文档空间和活页页面 → 装备份定时
# 用法：bash deploy.sh            正常部署/更新
#       bash deploy.sh --no-setup  跳过腾讯文档/活页初始化
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
SEC=/data/cf-n8n-secrets
SYS=$SEC/system.env
WSEC=$SEC/worker/secrets.env
SETUP=1
[[ "${1:-}" == "--no-setup" ]] && SETUP=0

if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER="sudo docker"; fi
DC="$DOCKER compose"
say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

say "1/7 密钥文件"
mkdir -p "$SEC/worker"
chmod 700 "$SEC" "$SEC/worker"
if [[ ! -f "$WSEC" ]]; then
  printf 'DEEPSEEK_API_KEY=\nTDOCS_MCP_TOKEN=\nMOWEN_API_KEY=\nLIVEPAGE_TOKEN=\nTDOCS_SPACE_NAME=自动工作流\n' > "$WSEC"
  chmod 600 "$WSEC"
  echo "已建 $WSEC，请运行 cf-secret 填写 token"
fi
if [[ ! -f "$SYS" ]]; then
  rnd() { openssl rand -hex "${1:-24}"; }
  umask 077
  cat > "$SYS" <<EOF
POSTGRES_USER=n8n
POSTGRES_PASSWORD=$(rnd 18)
POSTGRES_DB=n8n
N8N_ENCRYPTION_KEY=$(rnd 32)
N8N_OWNER_EMAIL=admin@cf.local
N8N_OWNER_PASSWORD=Cf$(rnd 8)9
WORKER_TOKEN=$(rnd 24)
SECRETS_DIR=$SEC/worker
EOF
  umask 022
  echo "已生成 $SYS"
fi
ln -sfn "$SYS" "$ROOT/.env"
set -a; . "$SYS"; set +a
ln -sfn "$ROOT/tools/set_secret.sh" /usr/local/bin/cf-secret 2>/dev/null || sudo ln -sfn "$ROOT/tools/set_secret.sh" /usr/local/bin/cf-secret || true

say "2/7 生成工作流 JSON"
python3 tools/build_workflows.py
mkdir -p assets backups logs

say "3/7 构建并启动容器"
$DC build worker
$DC up -d
wait_url() {
  for _ in $(seq 1 60); do curl -fsS -m 3 "$1" >/dev/null 2>&1 && return 0; sleep 3; done
  echo "等待 $1 超时"; return 1
}
wait_url http://127.0.0.1:8000/health
wait_url http://127.0.0.1:5678/healthz

say "4/7 n8n 管理员账号"
code=$(curl -s -o /tmp/cf_owner.json -w '%{http_code}' -X POST http://127.0.0.1:5678/rest/owner/setup \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$N8N_OWNER_EMAIL\",\"firstName\":\"CF\",\"lastName\":\"Admin\",\"password\":\"$N8N_OWNER_PASSWORD\"}")
if [[ "$code" == 200 ]]; then echo "已创建：$N8N_OWNER_EMAIL（密码见 $SYS）"; else echo "已存在或跳过（HTTP $code）"; fi
rm -f /tmp/cf_owner.json

say "5/7 导入并发布工作流"
$DC exec -T n8n n8n import:workflow --separate --input=/workflows
for f in n8n/workflows/*.json; do
  id=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" "$f")
  $DC exec -T n8n n8n publish:workflow --id="$id" >/dev/null && echo "已发布 $(basename "$f")"
done
$DC restart n8n
wait_url http://127.0.0.1:5678/healthz

say "6/7 初始化腾讯文档空间和活页页面"
if [[ $SETUP == 1 ]]; then
  curl -sS -m 600 -X POST "http://127.0.0.1:8000/jobs/setup?wait=true" -H "X-Worker-Token: $WORKER_TOKEN" \
    | python3 -m json.tool --no-ensure-ascii || true
fi

say "7/7 每日备份（01:00，保留 7 天）"
CRON="0 1 * * * root bash $ROOT/tools/backup.sh >> $ROOT/logs/backup.log 2>&1"
if [[ -w /etc/cron.d ]]; then echo "$CRON" > /etc/cron.d/cf-n8n; else echo "$CRON" | sudo tee /etc/cron.d/cf-n8n >/dev/null; fi

say "完成"
echo "n8n:    http://127.0.0.1:5678 （账号 $N8N_OWNER_EMAIL，密码在 $SYS）"
echo "状态:   curl -s -H 'X-Worker-Token: \$WORKER_TOKEN' http://127.0.0.1:8000/status"
echo "手动跑: curl -s -X POST -H 'X-Worker-Token: \$WORKER_TOKEN' 'http://127.0.0.1:8000/jobs/collect?group=G1&wait=true'"
