from celery import Celery

from app.config import settings


celery_app = Celery(
    "financial_analytics",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    enable_utc=True,
    timezone="UTC",
    imports=("app.api.tasks",),
)
