import logging
import multiprocessing
import os
import queue
import signal
import threading
import time
import portalocker
from apscheduler.schedulers.background import BackgroundScheduler
from .adapters import adapter, SourceError
from .config import Settings
from .db import Database
from .services import claim, complete, heartbeat, initialize, schedule

log = logging.getLogger("collector")


def execute(settings, envelope):
    job = envelope["job"]
    source = adapter(settings, job["source_id"])
    if job["kind"] == "discover":
        return source.discover(envelope["feed"], job["payload"].get("offset", 0))
    if job["kind"] == "fetch":
        return source.fetch(envelope["article"])
    if job["kind"] == "import":
        return source.fetch({"url": job["payload"]["url"]})
    if job["kind"] == "archive_image":
        return source.image(job["payload"]["url"])
    raise SourceError("UNSUPPORTED_JOB")


def child(settings, envelope, output, parent_pid):
    if os.name == "posix":
        os.setsid()
        def watch_parent():
            while True:
                if os.getppid() != parent_pid:
                    os.killpg(os.getpid(), signal.SIGKILL)
                    return
                time.sleep(0.2)
        threading.Thread(target=watch_parent, daemon=True).start()
    output.put({"ready": True})
    try:
        output.put({"result": execute(settings, envelope)})
    except SourceError as exc:
        output.put({"error": {"code": exc.code, "message": str(exc), "retry_after": exc.retry_after}})
    except BaseException as exc:
        # Never serialize exception strings that could include credential-bearing request URLs.
        output.put({"error": {"code": "INTERNAL_ERROR", "message": f"采集器异常：{type(exc).__name__}"}})


def stop_process(process, own_group=False):
    if process.pid and os.name == "posix":
        try:
            if own_group or os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            elif process.is_alive():
                process.terminate()
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(2)
    if own_group and os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.is_alive():
        if os.name == "posix" and os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.join(2)


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings)
        initialize(self.db)

    def once(self, subprocess=True):
        # The same file mutex is used by QR login/search and backups.
        try:
            with portalocker.Lock(str(self.settings.data_dir / "source.lock"), timeout=0):
                envelope = claim(self.db)
                if not envelope:
                    return False
                job = envelope["job"]
                outcome = None
                if subprocess:
                    context = multiprocessing.get_context("spawn")
                    output = context.Queue()
                    process = context.Process(target=child, args=(self.settings, envelope, output, os.getpid()))
                    process.start()
                    start = last_beat = time.monotonic()
                    own_group = False
                    try:
                        while time.monotonic() - start < self.settings.timeout:
                            try:
                                message = output.get(timeout=0.2)
                                if message.get("ready"):
                                    own_group = os.name == "posix"
                                    continue
                                outcome = message
                                break
                            except queue.Empty:
                                if not process.is_alive():
                                    outcome = {"error": {"code": "INTERNAL_ERROR", "message": "采集子进程意外退出"}}
                                    break
                            if time.monotonic() - last_beat >= 5:
                                if not heartbeat(self.db, job["id"], job["lease_token"]):
                                    outcome = {"error": {"code": "CANCELLED", "message": "任务已取消"}}
                                    break
                                last_beat = time.monotonic()
                        if outcome is None:
                            outcome = {"error": {"code": "NETWORK_ERROR", "message": "任务超时，已终止采集进程"}}
                    finally:
                        stop_process(process, own_group=own_group)
                        output.close()
                else:
                    try:
                        outcome = {"result": execute(self.settings, envelope)}
                    except SourceError as exc:
                        outcome = {"error": {"code": exc.code, "message": str(exc)}}
                complete(self.db, job["id"], job["lease_token"], **outcome)
                log.info("job_finished id=%s kind=%s", job["id"], job["kind"])
                return True
        except portalocker.exceptions.LockException:
            return False

    def run(self):
        with portalocker.Lock(str(self.settings.data_dir / "worker.lock"), timeout=0):
            # A crashed worker's child cannot run concurrently: source.lock must also be available.
            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(schedule, "interval", seconds=60, args=[self.db], max_instances=1, coalesce=True)
            if not self.settings.mock:
                from .backup import daily_backup
                scheduler.add_job(daily_backup, "cron", hour=3, minute=30, timezone="Asia/Shanghai", args=[self.settings], max_instances=1, coalesce=True)
            scheduler.start()
            schedule(self.db)
            try:
                while True:
                    try:
                        worked = self.once()
                    except Exception:
                        log.exception("Worker iteration failed")
                        worked = False
                    time.sleep(0.05 if worked and self.settings.mock else 1)
            finally:
                scheduler.shutdown(wait=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Worker(Settings()).run()
