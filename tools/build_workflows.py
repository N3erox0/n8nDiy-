#!/usr/bin/env python3
"""生成 n8n 工作流 JSON（n8n/workflows/*.json）。改定时就改这里，然后重新跑 deploy.sh。

n8n 只做定时触发：每个定时节点 → HTTP 调 worker 的 /jobs/<name>。逻辑全在 worker 里。
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
WORKER = "http://worker:8000"

WORKFLOWS = [
    ("cfWF01collect001", "WF-01 分组采集", True, [
        ("G1 官方与英文", "30 6,16 * * *", "collect?group=G1"),
        ("G2 关键词与X", "45 6,16 * * *", "collect?group=G2"),
        ("G3 媒体与公众号", "0 7,17 * * *", "collect?group=G3"),
    ]),
    ("cfWF02manual0001", "WF-02 收素材", False, [("每10分钟", "*/10 * * * *", "manual")]),
    ("cfWF03process001", "WF-03 选题加工", True, [("07:30 / 17:30", "30 7,17 * * *", "process")]),
    ("cfWF04card000001", "WF-04 选题卡（活页推送）", True, [("08:00 / 18:00", "0 8,18 * * *", "card")]),
    ("cfWF05generate01", "WF-05 图文生成（墨问草稿）", False, [("每5分钟", "*/5 * * * *", "generate")]),
    ("cfWF06revise0001", "WF-06 改稿与定稿", False, [("每5分钟", "2-59/5 * * * *", "revise")]),
    ("cfWF07cleanup001", "WF-07 清理", True, [("每天00:30", "30 0 * * *", "cleanup")]),
]


def build(wid: str, name: str, keep_success: bool, triggers: list[tuple[str, str, str]]) -> dict:
    nodes, conns = [], {}
    for i, (label, cron, path) in enumerate(triggers):
        y = 200 + i * 200
        t_name, h_name = f"定时 {label}", f"调用 worker {label}"
        nodes.append({
            "id": f"{wid}-t{i}", "name": t_name, "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2,
            "position": [200, y],
            "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": cron}]}},
        })
        nodes.append({
            "id": f"{wid}-h{i}", "name": h_name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": [480, y], "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000,
            "parameters": {
                "method": "POST", "url": f"{WORKER}/jobs/{path}",
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "X-Worker-Token", "value": "={{ $env.WORKER_TOKEN }}"}]},
                "options": {"timeout": 30000},
            },
        })
        conns[t_name] = {"main": [[{"node": h_name, "type": "main", "index": 0}]]}
    return {
        "id": wid, "name": name, "active": False, "nodes": nodes, "connections": conns,
        "settings": {"executionOrder": "v1", "timezone": "Asia/Shanghai",
                     "saveDataSuccessExecution": "all" if keep_success else "none",
                     "saveDataErrorExecution": "all", "saveManualExecutions": True},
        "pinData": {}, "tags": [],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    for wid, name, keep, triggers in WORKFLOWS:
        fn = OUT / f"{name.split(' ')[0]}.json"
        fn.write_text(json.dumps(build(wid, name, keep, triggers), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
        print("wrote", fn.relative_to(OUT.parent.parent))


if __name__ == "__main__":
    main()
