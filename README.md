# 内容工厂（n8n + worker）

AI 科技公众号的自动化流水线：定时采集 → 打分入库 → 聚类出题 → 活页选题卡推送到微信 → 你确认 → DeepSeek 生文写进墨问 → 你在墨问改稿 → 定稿归档到腾讯文档。

| 放在哪 | 放什么 |
|---|---|
| 活页「内容工厂 · 选题卡」 | 每天 08:00 / 18:00 的选题卡 + 「确认选题」表单，服务号推送 |
| 活页「内容工厂 · 改稿台」 | 新草稿 / 改稿完成 / 定稿 / 出错提醒 + 「改稿」表单，服务号推送 |
| 墨问 | 草稿、改稿（标签：草稿 / 定稿 / 已放弃） |
| 腾讯文档空间「自动工作流」 | 素材文件夹（含官方原件）、选题卡归档、发稿文件夹（定稿）、素材收集表、选题登记表、发文登记表 |
| 服务器 `assets/` | 原文 HTML、正文、图片备份 |

## 组成

- `docker-compose.yml`：Postgres 16、n8n 2.44.5、worker（FastAPI）、RSSHub + Redis。端口都只绑 127.0.0.1。
- `n8n/workflows/`：7 个工作流，只做定时触发，由 `tools/build_workflows.py` 生成。
- `worker/app/`：全部业务逻辑。`jobs.py` 是各工作流的编排，`main.py` 是 HTTP 入口。
- `worker/prompts/`：所有提示词；`worker/config/sources.yaml`：订阅源。

| 工作流 | 时间 | 做什么 |
|---|---|---|
| WF-01 采集 | 06:30/06:45/07:00、16:30/16:45/17:00 | 三组源分开采，每组 15 分钟超时 |
| WF-02 手工补全 | 每 10 分钟 | 素材收集表里「待补全」的链接/手工内容 |
| WF-03 选题加工 | 07:30 / 17:30 | 打分、入库、聚类、出题、存官方原件 |
| WF-04 选题卡 | 08:00 / 18:00 | 发活页选题卡并推送 |
| WF-05 生文 | 每 5 分钟（7–23 点） | 读确认 → 生成 → 写墨问 |
| WF-06 改稿 | 每 5 分钟（7–23 点） | 读改稿台/发文登记表的信号 → 改稿 / 定稿 / 放弃 |
| WF-07 清理 | 00:30 | 过期版本、作废选题、旧暂存 |

## 部署

```bash
bash deploy.sh          # 可重复跑：生成系统密钥、起容器、建 n8n 管理员、导入发布工作流、初始化腾讯文档和活页、装备份定时
cf-secret               # 填/改第三方 token（输入不回显，回车保留原值）
```

密钥都在仓库外：`/data/cf-n8n-secrets/system.env`（系统生成）和 `/data/cf-n8n-secrets/worker/secrets.env`（你填的 token，worker 每次调用时重读，改完不用重启）。

## 日常命令

```bash
bash tools/cf.sh status             # 密钥、表格/页面链接、墨问今日额度、各任务最近一次结果
bash tools/cf.sh run collect G1     # 立即跑一个任务并等结果
bash tools/cf.sh run process
bash tools/cf.sh logs
```

## 已知限制（10-29 实测）

- 墨问每个接口每天都有额度，免费版 `note/create` 10 次、`note/info` 10 次。所以系统**不轮询墨问**：改完稿一定要在改稿台或发文登记表里点「待修改 / 确认 / 放弃」。开通 Pro 后在 secrets.env 里加 `MOWEN_PRO=1`。
- X（推特）：服务器上 Bing 的 `site:` 检索失效，免登录镜像全部连不上，X 源目前都记为失败。
- Bing 关键词搜索结果常常没有发布时间，会被「缺必填」过滤掉；HN 关键词正常。
- 公众号只有 Wechat2RSS 免费清单里的 7 个号能采，其余标「无订阅方式」。

## 测试

```bash
docker compose run --rm --no-deps -v "$PWD/worker:/app" worker \
  sh -c 'TEST_DATABASE_URL=${DATABASE_URL%/cf}/cf_test python -m pytest -q'
```

包含一条端到端流程测试（真实数据库，假的 LLM / 墨问 / 活页）：打分 → 出题 → 选题卡 → 确认 → 生文 → 改稿 → 定稿 → 放弃 → 提醒。
