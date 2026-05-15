import os
import sqlite3
import sys
import tempfile
from pathlib import Path

tmp = tempfile.NamedTemporaryFile(delete=False)
tmp.close()
os.environ["DB_PATH"] = tmp.name
os.environ.setdefault("API_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot


def cleanup():
    try:
        bot.scheduler.shutdown(wait=False)
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(tmp.name + suffix)
        except FileNotFoundError:
            pass


try:
    bot.init_db()
    with sqlite3.connect(bot.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO allergies (date, time, severity, category, trigger_name, symptoms, medication, training_impact, missed_training, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-05-14", "7:30 AM", "moderate", "nasal/sinus", "pollen", "stuffy", "zyrtec", "modified", 0, "2026-05-14T07:30:00-04:00"),
        )
        conn.execute(
            "INSERT INTO allergies (date, time, severity, category, trigger_name, symptoms, medication, training_impact, missed_training, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-05-14", "evening class", "bad", "breathing", "dust", "wheezing", "inhaler", "skipped", 1, "2026-05-14T19:30:00-04:00"),
        )
        conn.execute(
            "INSERT INTO allergies (date, time, severity, category, trigger_name, symptoms, medication, training_impact, missed_training, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-05-14", "10:45 pm", "mild", "eyes", "mold", "itchy", "drops", "none", 0, "2026-05-14T22:45:00-04:00"),
        )
        conn.commit()

    logs = bot.get_all_allergies()
    assert len(logs) == 3
    assert any(log["missed_training"] is True for log in logs)

    stats = bot.get_allergy_stats()
    assert stats["total"] == 3
    assert stats["missed_training"] == 1
    assert stats["time_buckets"]["morning"] == 1
    assert stats["time_buckets"]["evening"] == 1
    assert stats["time_buckets"]["night"] == 1
    assert stats["top_triggers"][0]["count"] == 1
    assert any(row["name"] == "breathing" for row in stats["categories"])

    assert bot._infer_allergy_time_bucket("10:45 pm", None) == "night"
    assert bot._infer_allergy_time_bucket("noon after lunch", None) == "afternoon"

    client = bot.app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    create = client.post(
        "/api/allergies",
        json={
            "date": "2026-05-15",
            "time": "8pm class",
            "severity": "bad",
            "category": "eyes",
            "trigger": "mold",
            "symptoms": "itchy eyes",
            "medication": "drops",
            "training_impact": "skipped",
            "missed_training": True,
        },
        headers=headers,
    )
    assert create.status_code == 200
    created = create.get_json()
    assert created["ok"] is True
    assert isinstance(created["id"], int)

    empty_create = client.post("/api/allergies", json={"category": "general"}, headers=headers)
    assert empty_create.status_code == 400

    noop_update = client.patch(f"/api/allergies/{created['id']}", json={}, headers=headers)
    assert noop_update.status_code == 200

    update = client.patch(
        f"/api/allergies/{created['id']}",
        json={"trigger": "dust", "missed_training": False},
        headers=headers,
    )
    assert update.status_code == 200

    missing_update = client.patch("/api/allergies/999999", json={"trigger": "x"}, headers=headers)
    assert missing_update.status_code == 404

    delete = client.delete(f"/api/allergies/{created['id']}", headers=headers)
    assert delete.status_code == 200

    missing_delete = client.delete("/api/allergies/999999", headers=headers)
    assert missing_delete.status_code == 404
    print("All allergy stat tests passed")
finally:
    cleanup()
