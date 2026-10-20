"""Explicit real-model Qinghe regression, with isolated API/storage and no browser."""
import argparse
from copy import deepcopy
import json
import os
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUESTION = ("For the Qinghe Project, what was the official start date, who is the project manager, "
    "what is the first pilot city, and what is the approved total budget? "
    "Combine the available evidence and cite the source for each fact.")
MATERIALS = [
    ("qinghe_launch.txt", "Qinghe Project Launch Record\n\nThe Qinghe Project officially started on 18 April 2026.\n"
        "The project manager is Chen Yu.\nThe project is organized by Starbridge Research Center.\n"),
    ("qinghe_implementation.txt", "Qinghe Project Implementation Plan\n\nThe first pilot city of the Qinghe Project is Suzhou.\n"
        "The approved total budget of the Qinghe Project is CNY 1,200,000.\n"
        "The pilot activities include equipment deployment, staff training, and evaluation.\n"),
]
PROMPTS = [
    "You specialize in project launch records.\nUse only the supplied evidence.\n"
    "Identify the project's official start date and project manager.\n"
    "Answer the parts supported by your evidence and cite the original text.\n"
    "Explicitly identify any requested information missing from your evidence.\nDo not guess.",
    "You specialize in project implementation plans.\nUse only the supplied evidence.\n"
    "Identify the project's first pilot city and approved total budget.\n"
    "Answer the parts supported by your evidence and cite the original text.\n"
    "Explicitly identify any requested information missing from your evidence.\nDo not guess.",
]

def has_fact(answer, fact):
    if fact == "18 April 2026":
        return bool(re.search(r"18\s+April\s+2026|April\s+18,?\s+2026|2026\s*年\s*4\s*月\s*18\s*日|2026-04-18", answer, re.I))
    if fact == "1,200,000":
        return "1200000" in answer.replace(",", "")
    return fact.lower() in answer.lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-model-calls", action="store_true", required=True)
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--output", type=Path, default=ROOT / ".pytest-live-qinghe-fix/results.json")
    args = parser.parse_args()
    work = ROOT / ".pytest-live-qinghe-fix"
    work.mkdir(exist_ok=True)
    os.environ.update(MAQA_KNOWLEDGE_DIR=str(work / "knowledge"), MAQA_SYSTEMS_DIR=str(work / "systems"),
        MAQA_RUN_DB_PATH=str(work / "runs.sqlite3"), MAQA_EXPERIMENT_CATALOG_PATH=str(work / "none.json"))
    from fastapi.testclient import TestClient
    from backend.app import app
    report = {"model": args.model, "browser_tested": False, "runs": []}
    def persist():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with TestClient(app) as client:
        def request(method, path, **kw):
            response = client.request(method, "/api/v1" + path, **kw)
            response.raise_for_status()
            return response.json()
        templates = {item["agent_type"]: item["defaults"] for item in request("GET", "/agent-templates")["items"]}
        sources = [request("POST", "/knowledge/sources", data={"name": filename},
            files=[("files", (filename, text.encode(), "text/plain"))])["source_id"] for filename, text in MATERIALS]
        agents = []
        for role, key, index in [("planner", "planner", None), ("expert", "expert_a", 0),
                                  ("expert", "expert_b", 1), ("answer_validator", "reviewer", None)]:
            agent = deepcopy(templates[role])
            agent.update(agent_id=key, name=key)
            agent["model_config"]["model_name"] = args.model
            if index is not None:
                agent.update(source_id=sources[index], source_version=1, top_k=3)
                agent["model_config"]["system_prompt"] = PROMPTS[index]
            agents.append(agent)
        fields = {"name": "Qinghe output repair acceptance", "workflow_id": "multi_expert",
                  "agents": agents, "execution_mode": "sequential"}
        system = request("POST", "/systems", json=fields)
        for mode in ("sequential", "parallel"):
            if mode == "parallel":
                fields["execution_mode"] = mode
                system = request("PUT", f"/systems/{system['system_id']}/draft",
                    json={**fields, "expected_revision": system["draft_revision"]})
            response = client.post("/api/v1/qa/stream", json={"question": QUESTION,
                "system_id": system["system_id"], "draft_revision": system["draft_revision"]})
            response.raise_for_status()
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            detail = request("GET", f"/runs/{events[0]['run_id']}")
            report["runs"].append({"mode": mode, **detail})
            persist()
            record = detail["run"]
            assert record["status"] == "completed", record.get("error")
            final = record["final_state"]
            assert final["verified"], (final.get("answer"), final.get("verification_note"))
            answer = final["answer"]
            assert all(has_fact(answer, fact) for fact in ("18 April 2026", "Chen Yu", "Suzhou", "1,200,000")), answer
            assert "CNY" in answer or "RMB" in answer, answer
            assert {e["filename"] for e in final["supporting_evidence"]} == {m[0] for m in MATERIALS}
            for key, facts, filename in [("expert_a", ("18 April 2026", "Chen Yu"), MATERIALS[0][0]),
                                          ("expert_b", ("Suzhou", "1,200,000"), MATERIALS[1][0])]:
                result = final["expert_results"][key]
                assert result["failure_type"] != "output_error" and result["answer"], result
                assert all(has_fact(result["answer"], fact) for fact in facts), (key, result["answer"])
                assert result["supporting_evidence"]
                assert all(e["filename"] == filename for e in result["supporting_evidence"])
            starts = {e["node"]: e["timestamp"] for e in detail["events"] if e["event_type"] == "node_started"}
            ends = {e["node"]: e["timestamp"] for e in detail["events"] if e["event_type"] == "node_completed"}
            assert starts["reviewer"] > max(ends["expert_a"], ends["expert_b"])
            assert sum(e["event_type"] == "node_started" and e["node"] == "reviewer" for e in detail["events"]) == 1
            if mode == "parallel":
                assert max(starts["expert_a"], starts["expert_b"]) < min(ends["expert_a"], ends["expert_b"])
            usage = record["metrics"]["agent_usage"]
            assert sum(value["call_count"] for value in usage.values()) == record["metrics"]["llm_call_count"]
            assert all(len(value["calls"]) == value["call_count"] for value in usage.values())
            print(json.dumps({"mode": mode, "run_id": record["context"]["run_id"],
                "duration_ms": record["metrics"]["duration_ms"], "calls": record["metrics"]["llm_call_count"],
                "answer": answer}), flush=True)
        report["passed"] = True
        persist()


if __name__ == "__main__":
    main()
