from app.config import Settings


def test_scheduler_defaults():
    s = Settings(_env_file=None)
    assert s.scheduler_enabled is True
    assert s.scheduler_tick_seconds == 900
    assert s.scheduler_lock_ttl_seconds == 600
    assert s.scheduler_max_orgs_per_tick == 25


def test_scheduler_overrides_from_env(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_TICK_SECONDS", "60")
    monkeypatch.setenv("SCHEDULER_MAX_ORGS_PER_TICK", "100")
    s = Settings(_env_file=None)
    assert s.scheduler_enabled is False
    assert s.scheduler_tick_seconds == 60
    assert s.scheduler_max_orgs_per_tick == 100
