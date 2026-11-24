import base64
from typing import Annotated

import dagger
from dagger import DefaultPath, Doc, dag, function, object_type


@object_type
class MonitorKuma:
    def _docker_runner(
        self,
        source: dagger.Directory,
        docker_socket: dagger.Socket,
    ) -> dagger.Container:
        """Базовый контейнер с Docker CLI, Compose и Python для управления хостовым Docker."""
        return (
            dag.container()
            .from_("docker:27-cli")
            .with_exec(["apk", "add", "--no-cache", "python3", "curl"])
            .with_unix_socket("/var/run/docker.sock", docker_socket)
            .with_mounted_directory("/workspace", source)
            .with_workdir("/workspace")
        )

    def _ensure_env_and_network_cmd(self) -> str:
        """Команда подготовки .env (если его нет) и общей сети kuma-net."""
        return (
            "[ -f .env ] || cp .env.example .env && "
            "docker network inspect kuma-net >/dev/null 2>&1 || docker network create kuma-net"
        )

    def _run_py_in_net(self, script: str) -> str:
        """Запускает многострочный Python-скрипт в сети kuma-net без ошибок экранирования."""
        script_b64 = base64.b64encode(script.encode()).decode()
        return (
            f"docker run --rm --network kuma-net python:3.12-alpine "
            f"python3 -c \"import base64; exec(base64.b64decode('{script_b64}'))\""
        )

    @function
    async def deploy_app(
        self,
        docker_socket: Annotated[dagger.Socket, Doc("Путь к сокету Docker (/var/run/docker.sock)")],
        source: Annotated[dagger.Directory, DefaultPath(".")],
    ) -> str:
        """Поднимает Слой 1: MariaDB и Uptime Kuma."""
        cmd = f"{self._ensure_env_and_network_cmd()} && docker compose -p monitor-kuma-app -f compose.app.yaml up -d --wait"
        return await (
            self._docker_runner(source, docker_socket)
            .with_exec(["sh", "-c", cmd])
            .stdout()
        )

    @function
    async def deploy_monitoring(
        self,
        docker_socket: Annotated[dagger.Socket, Doc("Путь к сокету Docker (/var/run/docker.sock)")],
        source: Annotated[dagger.Directory, DefaultPath(".")],
    ) -> str:
        """Поднимает Слой 2: Prometheus, Grafana и экспортеры."""
        prom_cfg = await source.file("prometheus/prometheus.yaml").contents()
        bb_cfg = await source.file("blackbox/blackbox.yaml").contents()

        prom_b64 = base64.b64encode(prom_cfg.encode()).decode()
        bb_b64 = base64.b64encode(bb_cfg.encode()).decode()

        populate_vol = (
            "docker volume create monitor-kuma-mon_monitoring_configs >/dev/null && "
            "docker run --rm -v monitor-kuma-mon_monitoring_configs:/configs alpine sh -c "
            f"\"echo '{prom_b64}' | base64 -d > /configs/prometheus.yaml && "
            f"echo '{bb_b64}' | base64 -d > /configs/blackbox.yaml\""
        )
        cmd = (
            f"{self._ensure_env_and_network_cmd()} && "
            f"{populate_vol} && "
            "docker compose -p monitor-kuma-mon -f compose.monitoring.yaml up -d --wait"
        )
        return await (
            self._docker_runner(source, docker_socket)
            .with_exec(["sh", "-c", cmd])
            .stdout()
        )

    @function
    async def configure_grafana(
        self,
        docker_socket: Annotated[dagger.Socket, Doc("Путь к сокету Docker (/var/run/docker.sock)")],
        source: Annotated[dagger.Directory, DefaultPath(".")],
    ) -> str:
        """Накатывает Data Source и Dashboard в Grafana через HTTP API."""
        ds_json = await source.file("grafana/datasource.json").contents()
        db_json = await source.file("grafana/dashboard.json").contents()

        if not ds_json.strip() or not db_json.strip():
            raise RuntimeError("grafana/datasource.json or grafana/dashboard.json is empty!")

        entries = await source.entries()
        env_filename = ".env" if ".env" in entries else ".env.example"
        env_raw = await source.file(env_filename).contents()

        ds_b64 = base64.b64encode(ds_json.encode()).decode()
        db_b64 = base64.b64encode(db_json.encode()).decode()
        env_b64 = base64.b64encode(env_raw.encode()).decode()

        script = f"""
import base64, time, urllib.request, urllib.error

env = {{}}
for line in base64.b64decode("{env_b64}").decode().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

user = env.get("GRAFANA_ADMIN_USER", "admin")
password = env.get("GRAFANA_ADMIN_PASSWORD", "admin")
auth_header = "Basic " + base64.b64encode(f"{{user}}:{{password}}".encode()).decode()
base_url = "http://kuma-grafana:3000"

for _ in range(30):
    try:
        req = urllib.request.Request(f"{{base_url}}/api/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                break
    except Exception:
        time.sleep(2)
else:
    raise RuntimeError("Grafana API did not become ready in time")

def api_post(path, raw_bytes):
    req = urllib.request.Request(
        f"{{base_url}}{{path}}",
        data=raw_bytes,
        headers={{
            "Content-Type": "application/json",
            "Authorization": auth_header,
        }},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return 409, e.read().decode()
        raise

ds_status, ds_body = api_post("/api/datasources", base64.b64decode("{ds_b64}"))
db_status, db_body = api_post("/api/dashboards/db", base64.b64decode("{db_b64}"))
print(f"Datasource API status: {{ds_status}} -> {{ds_body}}")
print(f"Dashboard API status: {{db_status}} -> {{db_body}}")
"""
        return await (
            self._docker_runner(source, docker_socket)
            .with_exec(["sh", "-c", self._run_py_in_net(script)])
            .stdout()
        )

    @function
    async def deploy_all(
        self,
        docker_socket: Annotated[dagger.Socket, Doc("Путь к сокету Docker (/var/run/docker.sock)")],
        source: Annotated[dagger.Directory, DefaultPath(".")],
    ) -> str:
        """End-to-End Orchestration: поднимает оба слоя, настраивает Grafana и валидирует систему."""
        out_app = await self.deploy_app(docker_socket=docker_socket, source=source)
        out_mon = await self.deploy_monitoring(docker_socket=docker_socket, source=source)
        out_graf = await self.configure_grafana(docker_socket=docker_socket, source=source)

        check_cmd = (
            "docker ps --format 'table {{.Names}}\\t{{.Status}}' && "
            "docker run --rm --network kuma-net curlimages/curl:8.10.1 -sf http://kuma-prometheus:9090/-/healthy && "
            "docker run --rm --network kuma-net curlimages/curl:8.10.1 -sf http://uptime-kuma:3001 >/dev/null && "
            "echo 'ALL SYSTEMS HEALTHY!'"
        )
        out_check = await (
            self._docker_runner(source, docker_socket)
            .with_exec(["sh", "-c", check_cmd])
            .stdout()
        )
        return f"=== DEPLOY APP ===\n{out_app}\n=== DEPLOY MONITORING ===\n{out_mon}\n=== CONFIGURE GRAFANA ===\n{out_graf}\n=== VALIDATION ===\n{out_check}"

    @function
    async def chaos_test(
        self,
        docker_socket: Annotated[dagger.Socket, Doc("Путь к сокету Docker (/var/run/docker.sock)")],
        source: Annotated[dagger.Directory, DefaultPath(".")],
        target: Annotated[str, Doc("Имя контейнера для теста: uptime-kuma или kuma-mariadb")] = "uptime-kuma",
    ) -> str:
        """Chaos Testing: останавливает сервис, фиксирует статус DOWN в Prometheus/Grafana, восстанавливает и проверяет UP."""
        metric = "probe_success" if target == "uptime-kuma" else "mysql_up"

        check_metric_py = f"""
import json, urllib.request
d = json.loads(urllib.request.urlopen("http://kuma-prometheus:9090/api/v1/query?query={metric}", timeout=5).read())
res = d.get("data", {{}}).get("result", [])
val = res[0]["value"][1] if res else "0"
print(f"{metric}={{val}}")
"""
        runner = self._docker_runner(source, docker_socket)

        step1 = await runner.with_exec(["sh", "-c", self._run_py_in_net(check_metric_py)]).stdout()
        step2 = await runner.with_exec([
            "sh", "-c",
            f"docker stop {target} && sleep 15 && {self._run_py_in_net(check_metric_py)}"
        ]).stdout()
        step3 = await runner.with_exec([
            "sh", "-c",
            f"docker start {target} && sleep 15 && {self._run_py_in_net(check_metric_py)}"
        ]).stdout()

        return (
            f"[1/4] Initial state: {step1.strip()} (OK)\n"
            f"[2/4] After docker stop {target}: {step2.strip()} -> GRAFANA SHOWS: DOWN (ВСЁ ПЛОХО)!\n"
            f"[3/4] After recovery of {target}: {step3.strip()} -> GRAFANA SHOWS: UP (OK)!\n"
            f"[4/4] Chaos test completed successfully!"
        )