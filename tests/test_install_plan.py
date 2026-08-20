import pytest

from jnby_news_watch.install_plan import CronDesired, reconcile_cron


def exact_job(**updates):
    job = {
        "id": "job-1",
        "name": "JNBY Daily Intelligence",
        "schedule": "0 8 * * *",
        "script": "jnby-news-watch.py",
        "no_agent": True,
        "deliver": "feishu",
        "workdir": "E:\\My_workspace\\JNBY",
        "enabled": True,
    }
    job.update(updates)
    return job


def test_absent_job_plans_create_without_touching_unrelated_jobs():
    plan = reconcile_cron([{"id": "other", "name": "daily backup"}])
    assert plan.action == "create"
    assert plan.job_id is None


def test_exact_job_is_noop():
    plan = reconcile_cron([exact_job()])
    assert plan.action == "noop"
    assert plan.changes == ()


def test_drifted_job_plans_update_only_drifted_fields():
    plan = reconcile_cron([exact_job(schedule="0 9 * * *", no_agent=False)])
    assert plan.action == "update"
    assert plan.job_id == "job-1"
    assert plan.changes == ("schedule", "no_agent")


def test_duplicate_names_fail_closed_without_deletion():
    with pytest.raises(ValueError, match="multiple cron jobs"):
        reconcile_cron([exact_job(id="a"), exact_job(id="b")])


def test_custom_workspace_is_compared_exactly():
    desired = CronDesired(workdir="D:\\workspace\\DailyNews")
    plan = reconcile_cron([exact_job()], desired)
    assert plan.action == "update"
    assert plan.changes == ("workdir",)
