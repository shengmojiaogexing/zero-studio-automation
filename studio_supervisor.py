import os
import sys
import time
import json
import subprocess
import urllib.request

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
PID_FILE = os.path.join(LOGS_DIR, "studio_pids.json")
SUPERVISOR_LOG = os.path.join(LOGS_DIR, "studio_supervisor.log")

def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted, flush=True)
    with open(SUPERVISOR_LOG, "a", encoding="utf-8") as f:
        f.write(formatted + "\n")

def load_env():
    env_path = os.path.join(ROOT_DIR, ".env")
    env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    env[k] = v
    return env

SERVICES = {
    "booking-site": {
        "cmd": ["node", "server.js"],
        "cwd": os.path.join(ROOT_DIR, "apps", "booking-site"),
        "health_url": "http://127.0.0.1:3000/api/settings"
    },
    "im-gateway": {
        "cmd": [sys.executable, "main.py"],
        "cwd": os.path.join(ROOT_DIR, "services", "im-gateway"),
        "health_url": "http://127.0.0.1:8090/health"
    },
    "control-center": {
        "cmd": [sys.executable, "main.py"],
        "cwd": os.path.join(ROOT_DIR, "apps", "control-center"),
        "health_url": "http://127.0.0.1:3080/api/auth/status"
    },
    "frpc-tunnel": {
        "cmd": [
            os.path.join(ROOT_DIR, "infra", "tunnel", "bin", "frpc.exe"),
            "-c",
            os.path.join(ROOT_DIR, "infra", "tunnel", "frpc.toml")
        ],
        "cwd": os.path.join(ROOT_DIR, "infra", "tunnel"),
        "health_url": None
    },
    "retouch-worker": {
        "cmd": [
            sys.executable,
            os.path.join(ROOT_DIR, "pipelines", "retouch-engine", "worker.py"),
            "--interval",
            "10"
        ],
        "cwd": os.path.join(ROOT_DIR, "pipelines", "retouch-engine"),
        "health_url": None,
        "env_toggle": "ENABLE_RETOUCH_WORKER",
        "default_enabled": True
    },
    "card-watcher": {
        "cmd": [
            sys.executable,
            os.path.join(ROOT_DIR, "pipelines", "ingest-cull-cluster", "ingest.py"),
            "--watch",
            "--auto-cluster",
            "--auto-notify"
        ],
        "cwd": os.path.join(ROOT_DIR, "pipelines", "ingest-cull-cluster"),
        "health_url": None,
        "env_toggle": "ENABLE_CARD_WATCHER",
        "default_enabled": os.name == "nt"
    }
}

class Supervisor:
    def __init__(self):
        self.env = load_env()
        self.procs = {}
        self.files = {}

    def start_service(self, name: str):
        self.env = load_env()
        cfg = SERVICES[name]
        out_path = os.path.join(LOGS_DIR, f"{name}.stdout.log")
        err_path = os.path.join(LOGS_DIR, f"{name}.stderr.log")

        out_f = open(out_path, "a", encoding="utf-8", buffering=1)
        err_f = open(err_path, "a", encoding="utf-8", buffering=1)
        self.files[name] = (out_f, err_f)

        log(f"Starting {name} ({' '.join(cfg['cmd'])})...")
        p = subprocess.Popen(
            cfg["cmd"],
            cwd=cfg["cwd"],
            env=self.env,
            stdout=out_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL
        )
        self.procs[name] = p
        log(f"{name} started with PID {p.pid}")
        return p

    def save_pids(self):
        pids = {"supervisor": os.getpid()}
        for name, p in self.procs.items():
            pids[name] = p.pid
        with open(PID_FILE, "w", encoding="utf-8") as f:
            json.dump(pids, f, indent=4)

    def check_health(self, url: str) -> bool:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(url, headers={"User-Agent": "ZeroStudioSupervisor/1.0"})
            with opener.open(req, timeout=3) as resp:
                return resp.status in (200, 401)
        except Exception:
            return False

    def is_service_enabled(self, name: str) -> bool:
        cfg = SERVICES[name]
        toggle_key = cfg.get("env_toggle")
        if not toggle_key:
            return True
        val = self.env.get(toggle_key)
        if val is None or str(val).strip() == "":
            return bool(cfg.get("default_enabled", True))
        return str(val).strip().lower() not in ("false", "0", "no", "off")

    def run(self):
        log("Zero Studio Supervisor initializing...")
        for name in SERVICES:
            if not self.is_service_enabled(name):
                log(f"Skipping {name} (disabled via {SERVICES[name].get('env_toggle')})")
                continue
            self.start_service(name)
        self.save_pids()

        # Wait for initialization
        time.sleep(3)
        for name, cfg in SERVICES.items():
            if cfg["health_url"]:
                healthy = False
                for _ in range(15):
                    if self.check_health(cfg["health_url"]):
                        healthy = True
                        break
                    time.sleep(1)
                log(f"Health check for {name}: {'HEALTHY' if healthy else 'TIMEOUT/DEGRADED'}")

        log("Zero Studio full stack is running! Monitoring services...")
        try:
            while True:
                time.sleep(5)
                for name, p in list(self.procs.items()):
                    ret = p.poll()
                    if ret is not None:
                        log(f"WARNING: Service {name} exited with code {ret}! Restarting...")
                        self.start_service(name)
                        self.save_pids()
        except KeyboardInterrupt:
            log("Supervisor received shutdown signal. Terminating managed services...")
            for name, p in self.procs.items():
                p.terminate()
            for out_f, err_f in self.files.values():
                out_f.close()
                err_f.close()
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
            log("All services terminated.")

if __name__ == "__main__":
    Supervisor().run()
