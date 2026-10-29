#!/usr/bin/env bash
# 内容工厂命令行：
#   cf status                 看密钥、腾讯文档/活页链接、墨问今日额度、各任务最近一次结果
#   cf run <任务> [组]         立即跑一个任务并等结果，如：cf run collect G1 / cf run process / cf run card
#   cf start <任务> [组]       后台跑，不等
#   cf logs                   看 worker 最近日志
# 任务：collect manual process card generate revise cleanup setup selftest test_push
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
TOKEN=$(grep '^WORKER_TOKEN=' "$ROOT/.env" | cut -d= -f2-)
API=http://127.0.0.1:8000
H=(-H "X-Worker-Token: $TOKEN")
pretty() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }
case "${1:-status}" in
  status) curl -sS "${H[@]}" "$API/status" | pretty ;;
  run)    q=""; [[ -n "${3:-}" ]] && q="&group=$3"
          curl -sS -m 3600 -X POST "${H[@]}" "$API/jobs/$2?wait=true$q" | pretty ;;
  start)  q=""; [[ -n "${3:-}" ]] && q="?group=$3"
          curl -sS -X POST "${H[@]}" "$API/jobs/$2$q" | pretty ;;
  logs)   cd "$ROOT" && { docker compose logs --tail=200 worker 2>/dev/null || sudo docker compose logs --tail=200 worker; } ;;
  *)      sed -n '2,8p' "$0" ;;
esac
