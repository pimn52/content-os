"""Creator-scoped metric snapshots and reusable publication baselines."""
from __future__ import annotations

from statistics import median
from uuid import UUID

from app.db import ProjectRepository, PublicationMetricObservationRepository, PublicationRepository


def _numeric_metrics(values: dict[str, object]) -> dict[str, float]:
    return {
        key: float(value)
        for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def latest_metric_observation(db, publication_id: UUID, window_hours: int):
    values = [
        value for value in PublicationMetricObservationRepository(db).list_for_publication(publication_id)
        if value.window_hours == window_hours
    ]
    return values[-1] if values else None


def creator_publication_baseline(db, project_id: UUID, platform: str, window_hours: int) -> dict[str, object]:
    project = ProjectRepository(db).get(project_id)
    if project is None:
        raise LookupError("project not found")
    publications = [
        value for value in PublicationRepository(db).list_for_project(project_id)
        if value.platform == platform
    ]
    current = publications[-1] if publications else None
    if current is None:
        return {
            "state": "no_publication", "publication_id": None, "output_version": None,
            "platform": platform, "window_hours": window_hours, "sample_size": 0,
            "current_metrics": {}, "baseline_median": {}, "delta_percent": {}, "evidence_refs": [],
        }
    current_observation = latest_metric_observation(db, current.id, window_hours)
    if current_observation is None:
        return {
            "state": "no_current_observation", "publication_id": current.id,
            "output_version": current.output_version, "platform": platform,
            "window_hours": window_hours, "sample_size": 0, "current_metrics": {},
            "baseline_median": {}, "delta_percent": {}, "evidence_refs": [],
        }

    current_metrics = _numeric_metrics(current_observation.metrics)
    prior = []
    for publication in PublicationRepository(db).list():
        if publication.id == current.id or publication.platform != platform:
            continue
        prior_project = ProjectRepository(db).get(publication.project_id)
        if prior_project is None or prior_project.ip_profile_id != project.ip_profile_id:
            continue
        observation = latest_metric_observation(db, publication.id, window_hours)
        if observation is not None:
            prior.append(observation)

    common_keys = sorted(set(current_metrics).intersection(
        *(set(_numeric_metrics(value.metrics)) for value in prior)
    )) if prior else []
    baseline = {
        key: float(median(_numeric_metrics(value.metrics)[key] for value in prior))
        for key in common_keys
    }
    delta = {
        key: ((current_metrics[key] - baseline[key]) / baseline[key] * 100)
        if baseline[key] != 0 else None
        for key in common_keys
    }
    return {
        "state": "ready" if len(prior) >= 2 else "emerging" if len(prior) == 1 else "insufficient",
        "publication_id": current.id,
        "output_version": current.output_version,
        "platform": platform,
        "window_hours": window_hours,
        "sample_size": len(prior),
        "current_metrics": {key: current_metrics[key] for key in common_keys},
        "baseline_median": baseline,
        "delta_percent": delta,
        "evidence_refs": [f"metric_observation:{current_observation.id}",
                          *(f"metric_observation:{value.id}" for value in prior)],
    }
