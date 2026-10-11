from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Common configuration, read from RPM_* environment variables. The endpoint override points boto3 at the local
    emulator (ElasticMQ); leave it unset on AWS so the SDK uses the real endpoints and the instance role."""

    model_config = SettingsConfigDict(env_prefix="RPM_", extra="ignore")

    service_name: str = "rpm"
    env: str = "local"
    log_level: str = "INFO"
    metrics_port: int = 9100

    database_url: str = "postgresql://rpm:rpm@localhost:5432/rpm"

    aws_region: str = "ap-southeast-2"
    sqs_endpoint: str | None = None
    sqs_events_queue: str = "rpm-patient-events"   # the unified stream: device telemetry, device events, EHR events
    sqs_agent_queue: str = "rpm-agent-tasks"       # care-api -> agent-service

    care_api_url: str = "http://localhost:8000"
    risk_service_url: str = "http://localhost:8001"
    fhir_base_url: str = "http://localhost:8080/fhir"
    service_token: str = "dev-service-token"       # shared secret for service-to-service calls (/internal/*)

    # Does a device that stops reporting raise an alert (for an ICU patient)? Off: this is a simulated
    # demonstration, where devices are stopped and paused on purpose. With real monitors, turn it on.
    silent_device_alerts: bool = False
