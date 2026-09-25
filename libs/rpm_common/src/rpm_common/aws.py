import boto3
from botocore.config import Config

_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 10}, connect_timeout=5, read_timeout=15)


def client(service: str, region: str, endpoint: str | None = None):
    """boto3 client; `endpoint` targets a local emulator, None uses the real AWS endpoint."""
    kwargs = {"region_name": region, "config": _CONFIG}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client(service, **kwargs)
