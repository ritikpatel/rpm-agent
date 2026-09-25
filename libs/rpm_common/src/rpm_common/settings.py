from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common configuration, read from RPM_* environment variables. Endpoint overrides point boto3 at local
    emulators (kinesis-mock, ElasticMQ); leave them unset on AWS so the SDK uses real endpoints + pod identity."""

    model_config = SettingsConfigDict(env_prefix="RPM_", extra="ignore")

    service_name: str = "rpm"
    env: str = "local"
    log_level: str = "INFO"
    metrics_port: int = 9100

    database_url: str = "postgresql://rpm:rpm@localhost:5432/rpm"
    redis_url: str = "redis://localhost:6379/0"

    aws_region: str = "us-east-1"
    kinesis_endpoint: str | None = None
    sqs_endpoint: str | None = None
    kinesis_stream: str = "rpm-telemetry"
    sqs_clinical_events_queue: str = "rpm-clinical-events"
    sqs_alert_intake_queue: str = "rpm-alert-intake"

    fhir_base_url: str = "http://localhost:8080/fhir"
