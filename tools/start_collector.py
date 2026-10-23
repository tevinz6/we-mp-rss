"""Start the isolated web and worker services together for local use."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Use an isolated offline demonstration archive")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    env = dict(os.environ)
    if args.demo:
        env.update(COLLECTOR_MOCK="true", COLLECTOR_DATA_DIR=str(ROOT / "data/demo"), COLLECTOR_REQUEST_INTERVAL_SECONDS="0")
        subprocess.run([sys.executable, "-m", "core.collection.cli", "demo"], cwd=ROOT, env=env, check=True)
    command = [sys.executable, "-m", "core.collection.cli"]
    web = subprocess.Popen(command + ["serve", "--port", str(args.port)], cwd=ROOT, env=env)
    worker = subprocess.Popen(command + ["worker"], cwd=ROOT, env=env)
    print(f"工作台：http://127.0.0.1:{args.port} ；首次打开时设置管理员密码。", flush=True)
    try:
        while web.poll() is None and worker.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for process in (worker, web):
            if process.poll() is None:
                process.terminate()
        for process in (worker, web):
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
