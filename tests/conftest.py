import os

from hypothesis import HealthCheck, settings

os.environ.setdefault(
    "MDM_DATABASE_URL",
    "postgresql+asyncpg://mdm:mdm-local@127.0.0.1:55432/mdm_test",
)

settings.register_profile(
    "default",
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("default")
