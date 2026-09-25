"""`python -m rpm_common.migrate` — run by the compose `db-migrate` service and the Helm pre-install hook."""

import os

from .db import ensure_databases, migrate, wait_for_db
from .logging import setup_logging
from .settings import BaseServiceSettings


def main() -> None:
    s = BaseServiceSettings(service_name="db-migrate")
    setup_logging(s.service_name, s.log_level)
    wait_for_db(s.database_url)
    extra = [d for d in os.environ.get("RPM_CREATE_DATABASES", "").split(",") if d]
    if extra:
        print(f"databases created: {ensure_databases(s.database_url, extra) or 'none (exist)'}")
    applied = migrate(s.database_url)
    print(f"migrations applied: {applied or 'none (up to date)'}")


if __name__ == "__main__":
    main()
