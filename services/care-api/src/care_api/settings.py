from rpm_core.settings import BaseServiceSettings


class CareSettings(BaseServiceSettings):
    service_name: str = "care-api"
    host: str = "0.0.0.0"
    port: int = 8000
    run_migrations: bool = True

    # sign-in
    auth_mode: str = "oidc"          # oidc (Cognito) | dev (local only: API-minted tokens, same verification path)
    oidc_issuer: str | None = None   # https://cognito-idp.<region>.amazonaws.com/<user-pool-id>
    oidc_client_id: str | None = None
    oidc_domain: str | None = None   # hosted UI base, https://<prefix>.auth.<region>.amazoncognito.com

    # devices (clinician and agent messages to the patient's watch, simulator controls)
    device_command_mode: str = "mqtt"  # mqtt (local broker) | iot (AWS IoT data plane) | disabled
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    iot_endpoint: str | None = None
    demo_controls: bool = True         # scenario, discharge and lab controls for demonstrations

    # alerts and notifications
    escalation_policy_file: str | None = None
    care_team_file: str | None = None
    escalation_timeout_scale: float = 1.0
    notify_email: str | None = None    # override: send every tier's e-mail to these comma-separated addresses
    console_url: str = "http://localhost:3000"
    email_backend: str = "log"         # ses | smtp | log
    email_sender: str = "alerts@rpm.local"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    tick_s: float = 1.0
    dispatch_settle_s: float = 0.25
    agent_debounce_s: int = 600        # at most one agent run per patient in this window, unless severity rose

    # guideline search
    embeddings: str = "hash"           # hash (offline, deterministic) | titan (Amazon Bedrock)

    # FHIR
    fhir_writeback: bool = True
    fhir_writeback_interval_s: float = 5.0
