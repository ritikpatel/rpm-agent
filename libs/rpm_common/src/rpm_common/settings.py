from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common configuration, read from RPM_* environment variables. The endpoint override points boto3 at the local
    emulator (ElasticMQ); leave it unset on AWS so the SDK uses real endpoints + pod identity."""

    model_config = SettingsConfigDict(env_prefix="RPM_", extra="ignore")

    service_name: str = "rpm"
    env: str = "local"
    log_level: str = "INFO"
    metrics_port: int = 9100

    database_url: str = "postgresql://rpm:rpm@localhost:5432/rpm"
    redis_url: str = "redis://localhost:6379/0"

    aws_region: str = "ap-southeast-2"
    sqs_endpoint: str | None = None
    sqs_telemetry_queue: str = "rpm-telemetry"          # IoT rule -> stream-engine (real-time path)
    sqs_archive_queue: str = "rpm-telemetry-archive"    # IoT rule -> telemetry-archiver -> S3 lake ("" = off)
    sqs_clinical_events_queue: str = "rpm-clinical-events"
    sqs_alert_intake_queue: str = "rpm-alert-intake"

    fhir_base_url: str = "http://localhost:8080/fhir"
