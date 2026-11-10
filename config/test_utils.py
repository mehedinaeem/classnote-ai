"""Explicit eager execution helpers; production requests always use the broker."""
from django.test import Client, TestCase
from config.celery import app


class EagerClient(Client):
    def request(self, **request):
        with TestCase.captureOnCommitCallbacks(execute=True):
            return super().request(**request)


def enable_eager(testcase):
    values = {"task_always_eager": True, "task_eager_propagates": False,
              "broker_url": "memory://", "result_backend": "cache+memory://"}
    previous = {key: app.conf.get(key) for key in values}
    app.conf.update(values)
    testcase.addCleanup(lambda: app.conf.update(previous))
    testcase.client = EagerClient()
