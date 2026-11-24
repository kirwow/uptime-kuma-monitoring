# Локальный отказоустойчивый контур мониторинга для Uptime Kuma

Проект разворачивает продакшн-сервис **Uptime Kuma** на базе внешней БД **MariaDB** и независимый стек наблюдаемости (**Prometheus**, **Grafana** и набор экспортёров), управляемый декларативным **Dagger-модулем** на Python.

---

## 1. Архитектура решения

Инфраструктура разделена на два независимых слоя Docker Compose, объединённых общей внешней сетью `kuma-net`:

1. **Слой 1 — Приложение и БД (`compose.app.yaml`):**
   * **`mariadb` (`mariadb:10.11`)** — реляционная БД для хранения данных Uptime Kuma. Использует именованный том `mariadb_data` для персистентности данных, `healthcheck` и политику `restart: unless-stopped`.
   * **`uptime-kuma` (`louislam/uptime-kuma:2`)** — веб-сервис мониторинга, подключённый к MariaDB по внутренней сети. Запускается строго после прохождения healthcheck базы данных.

2. **Слой 2 — Наблюдаемость (`compose.monitoring.yaml`):**
   * **`prometheus` (`prom/prometheus:v2.54.1`)** — сбор и хранение метрик (интервал скрапинга `10s`).
   * **`grafana` (`grafana/grafana:11.2.2`)** — визуализация метрик. Data Source и дашборды накатываются программно через Grafana HTTP API.
   * **Экспортёры метрик:**
     * `cadvisor` (`gcr.io/cadvisor/cadvisor:v0.49.1`) — потребление CPU, RAM и сетевых ресурсов Docker-контейнеров.
     * `node-exporter` (`prom/node-exporter:v1.8.2`) — системные метрики хоста (Load Average, память, диски).
     * `mysqld-exporter` (`prom/mysqld-exporter:v0.15.1`) — состояние базы данных MariaDB (`mysql_up`, активные соединения).
     * `blackbox-exporter` (`prom/blackbox-exporter:v0.25.0`) — синтетические HTTP-пробы доступности и latency веб-интерфейса Uptime Kuma (`probe_success`, `probe_duration_seconds`).

3. **Автоматизация (Dagger.io):**
   * В корне репозитория расположен Dagger-модуль `monitor-kuma` (`dagger.json` + `.dagger/src/monitor_kuma/main.py`).
   * Управление хостовым Docker-демоном осуществляется через проброс Unix-сокета `/var/run/docker.sock`.

---

## 2. Системные требования и подготовка окружения

Для локального запуска потребуются:
* **ОС:** Linux, macOS или Windows 10/11 (WSL2)
* **Docker Engine / Docker Desktop** с запущенным демоном и доступным сокетом `/var/run/docker.sock`
* **Dagger CLI** (`v0.15+` / `v0.21+`)

### Установка Dagger CLI (если не установлен)
* **macOS (Homebrew):**
  ```bash
  brew install dagger/tap/dagger
  ```
* **Linux / WSL:**
  ```bash
  curl -fsSL https://dl.dagger.io/dagger/install.sh | BIN_DIR=/usr/local/bin sh
  ```

### Подготовка переменных окружения
Перед запуском вы можете скопировать шаблон переменных окружения (при запуске через Dagger файл `.env` создаётся автоматически из `.env.example`, если отсутствует):
```bash
cp .env.example .env
```

---

## 3. Запуск и управление через Dagger

Все команды выполняются из корневой директории проекта.

### Просмотр доступных функций модуля
```bash
dagger functions
```

### Пошаговое развёртывание (основные команды)

1. **Запуск Слоя 1 (MariaDB и Uptime Kuma):**
   ```bash
   dagger call deploy-app --docker-socket=/var/run/docker.sock
   ```

2. **Запуск Слоя 2 (Prometheus, Grafana и экспортеры):**
   ```bash
   dagger call deploy-monitoring --docker-socket=/var/run/docker.sock
   ```

3. **Настройка Grafana через HTTP API (создание Data Source и импорт дашборда):**
   ```bash
   dagger call configure-grafana --docker-socket=/var/run/docker.sock
   ```

### Бонусные сценарии

* **End-to-End Orchestration (развёртывание всей платформы одной командой и проверка здоровья всех систем):**
  ```bash
  dagger call deploy-all --docker-socket=/var/run/docker.sock
  ```

* **Chaos Testing (имитация отказа сервиса, проверка перехода дашборда в статус `DOWN (ВСЁ ПЛОХО)` и последующего восстановления в `UP`):**
  ```bash
  # Тест отказа веб-приложения Uptime Kuma (по умолчанию)
  dagger call chaos-test --docker-socket=/var/run/docker.sock

  # Тест отказа базы данных MariaDB
  dagger call chaos-test --docker-socket=/var/run/docker.sock --target=kuma-mariadb
  ```

---

## 4. Доступ к сервисам и проверка работоспособности

После успешного выполнения команд сервисы доступны локально по следующим адресам:

| Сервис | URL | Учётные данные по умолчанию |
| :--- | :--- | :--- |
| **Uptime Kuma** | http://localhost:3001 | Создание администратора при первом входе |
| **Grafana** | http://localhost:3000 | `admin` / `admin` (из `.env`) |
| **Prometheus** | http://localhost:9090 | Без авторизации |
| **cAdvisor** | http://localhost:8080 | Без авторизации |

### Чек-лист проверки компонентов

1. **Проверка контейнеров:**
   Выполните `docker ps` — все 8 контейнеров (`kuma-mariadb`, `uptime-kuma`, `kuma-prometheus`, `kuma-grafana`, `kuma-node-exporter`, `kuma-cadvisor`, `kuma-mysqld-exporter`, `kuma-blackbox-exporter`) должны находиться в статусе `Up` (а `kuma-mariadb` — в статусе `healthy`).
2. **Проверка сбора метрик в Prometheus:**
   Откройте http://localhost:9090/targets — все 5 target-групп (`prometheus`, `node-exporter`, `cadvisor`, `mariadb`, `blackbox-uptime-kuma`) должны иметь статус **`UP`**.
3. **Проверка дашборда в Grafana:**
   * Авторизуйтесь в http://localhost:3000 (`admin` / `admin`).
   * Перейдите в раздел **Dashboards** и откройте **Uptime Kuma & Infrastructure Health**.
   * Убедитесь, что виджеты **Uptime Kuma Status** и **MariaDB Database Status** отображают зелёный статус **`UP (OK)`**, а графики **HTTP Latency**, **Containers CPU Usage**, **Containers RAM Usage** и **MariaDB Connections & Host Load** строятся в реальном времени.

---

## 5. Остановка и очистка стенда

Для остановки контейнеров и удаления созданных ресурсов выполните:
```bash
docker compose -p monitor-kuma-mon -f compose.monitoring.yaml down -v
docker compose -p monitor-kuma-app -f compose.app.yaml down -v
docker network rm kuma-net
```