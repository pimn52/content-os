from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database, IPProfileRepository, ProjectRepository, PublicationRepository
from app.domain.models import IPProfile, Project, PublicationRecord, RationalFps
from app.main import create_app


def test_metric_snapshots_form_a_creator_platform_baseline_and_next_suggestion(tmp_path: Path):
    path = tmp_path / "publication-learning.sqlite3"
    now = datetime.now(timezone.utc)
    with Database(path) as db:
        creator = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
        other_creator = IPProfileRepository(db).create(IPProfile(creator_name="Other"))
        projects = [
            ProjectRepository(db).create(Project(
                ip_profile_id=creator.id, title=f"Creator {index}", topic="Philosophy",
                fps=RationalFps(numerator=30, denominator=1), created_at=now,
            ))
            for index in range(3)
        ]
        other_project = ProjectRepository(db).create(Project(
            ip_profile_id=other_creator.id, title="Other", topic="Philosophy",
            fps=RationalFps(numerator=30, denominator=1), created_at=now,
        ))
        publications = [
            PublicationRepository(db).create(PublicationRecord(
                project_id=project.id, output_version=f"render:{project.id}", platform="youtube",
                published_at=now - timedelta(days=index + 1), title=f"Video {index}",
            ))
            for index, project in enumerate(projects)
        ]
        PublicationRepository(db).create(PublicationRecord(
            project_id=other_project.id, output_version=f"render:{other_project.id}", platform="youtube",
            published_at=now - timedelta(days=4), title="Other creator",
        ))

    current = projects[-1]
    with TestClient(create_app(path)) as client:
        def record(publication, event_key, views, completion):
            response = client.post(
                f"/projects/{publication.project_id}/publications/{publication.id}/metric-observations",
                json={
                    "event_key": event_key, "observed_at": now.isoformat(), "window_hours": 24,
                    "metric_source": "manual-platform-export",
                    "metrics": {"views": views, "completion_rate": completion},
                },
            )
            assert response.status_code == 201, response.text
            return response.json()

        record(publications[0], "fixture-publication-prior-0", 100, 0.4)
        record(publications[1], "fixture-publication-prior-1", 200, 0.6)
        current_observation = record(publications[2], "fixture-publication-current", 240, 0.5)
        repeated = client.post(
            f"/projects/{current.id}/publications/{publications[2].id}/metric-observations",
            json={
                "event_key": "fixture-publication-current", "observed_at": now.isoformat(),
                "window_hours": 24, "metric_source": "manual-platform-export",
                "metrics": {"views": 240, "completion_rate": 0.5},
            },
        )
        assert repeated.status_code == 201
        assert repeated.json()["id"] == current_observation["id"]
        listed = client.get(
            f"/projects/{current.id}/publication-metric-observations?publication_id={publications[2].id}",
        )
        assert listed.status_code == 200 and len(listed.json()) == 1

        baseline = client.get(
            f"/projects/{current.id}/performance-baseline",
            params={"platform": "youtube", "window_hours": 24},
        )
        assert baseline.status_code == 200, baseline.text
        body = baseline.json()
        assert body["state"] == "ready" and body["sample_size"] == 2
        assert body["baseline_median"] == {"completion_rate": 0.5, "views": 150.0}
        assert body["current_metrics"] == {"completion_rate": 0.5, "views": 240.0}
        assert body["delta_percent"]["views"] == 60.0

        suggestions = client.get(f"/projects/{current.id}/next-suggestions").json()
        assert any("同创作者" in item["reason"] for item in suggestions)
        assert any(any(ref.startswith("metric_observation:") for ref in item["evidence_refs"])
                   for item in suggestions)

    with Database(path) as db:
        try:
            db.connection.execute(
                "UPDATE publication_metric_observations SET window_hours=48 WHERE id=?",
                (current_observation["id"],),
            )
        except Exception as error:
            assert "append-only" in str(error)
        else:
            raise AssertionError("metric observations must be append-only")
