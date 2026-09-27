"""Create one explicitly assisted new-topic R1 rehearsal draft via normal APIs."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.main import create_app  # noqa: E402

DATA_ROOT = ROOT / "content-os-data"
TITLE = "V57 辅助输入｜长采访短视频如何保住原意"
TOPIC = "把长采访剪成短视频，如何保住受访者原意"
COPY = (
    "把长采访剪成短视频，最容易犯的错，是先找一句听起来很炸的话。"
    "可一句话离开上下文，观点可能就变了。"
    "我的做法是先找一个能独立回答的问题，再保留支撑答案的证据。"
    "开头把问题抛给观众，中间只放一条最有力的例子，最后交代这段话原本在讨论什么。"
    "这样剪出来的短视频，不只是抓眼球，也不会把受访者的意思剪歪。"
)


def main() -> None:
    with TestClient(create_app(DATA_ROOT / "content-os.sqlite3")) as client:
        projects = client.get("/projects").json()
        matching = [item for item in projects if item["title"] == TITLE]
        if len(matching) > 1:
            raise ValueError("ambiguous V57 project")
        if matching:
            project = matching[0]
        else:
            response = client.post("/projects", json={
                "creator_name": "Local Research Creator", "title": TITLE, "topic": TOPIC,
            })
            if response.status_code != 201:
                raise ValueError(f"project creation failed: {response.status_code} {response.text}")
            project = response.json()
        project_id = project["id"]
        existing = client.get(f"/projects/{project_id}/draft")
        if existing.status_code != 200:
            raise ValueError(f"draft read failed: {existing.status_code} {existing.text}")
        draft = existing.json()
        if draft["script"] is None:
            response = client.put(f"/projects/{project_id}/draft", json={"script": COPY, "topic": TOPIC})
            if response.status_code != 200:
                raise ValueError(f"draft save failed: {response.status_code} {response.text}")
            draft = response.json()
        if draft["script"] != COPY or draft["topic"] != TOPIC:
            raise ValueError("the V57 assisted draft differs from the frozen copy")
        print(json.dumps({
            "project_id": project_id, "ip_profile_id": project["ip_profile_id"],
            "topic": TOPIC, "script": COPY, "script_length": len(COPY),
            "entry": "normal editable draft API",
            "evidence_class": "assisted editor input, not product scene-planner output",
        }))


if __name__ == "__main__":
    main()
