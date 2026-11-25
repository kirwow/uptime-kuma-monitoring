# MonitorKuma — Отказоустойчивый стенд мониторинга в Docker Compose и Kubernetes

Инфраструктурный проект, реализующий архитектуру **«мониторинга за мониторингом»** (Observability over Monitoring). Проект разворачивает систему проверки доступности сервисов **Uptime Kuma** с внешней реляционной БД **MariaDB** и независимый стек наблюдаемости на базе **Prometheus**, **Grafana** и специализированных экспортеров.

Инфраструктура описана декларативно в двух вариантах:
* **Docker Compose + Dagger** — модульное локальное развёртывание и программируемые CI/CD-пайплайны с автоматической настройкой дашбордов и Chaos-тестированием.
* **Kubernetes** — продакшн-ориентированная конфигурация с изоляцией слоёв по неймспейсам, персистентным хранением данных и самовосстановлением (Self-Healing).

---

## Архитектура проекта

Стенд разделён на два логически изолированных слоя (в Kubernetes им соответствуют отдельные `Namespace`):

```text
┌─────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                       │
│                                                             │
│  ┌───────────────────────────┐   ┌───────────────────────┐  │
│  │  Namespace: uptime-kuma   │   │ Namespace: monitoring │  │
│  │                           │   │                       │  │
│  │  ┌─────────────────────┐  │   │  ┌─────────────────┐  │  │
│  │  │  Uptime Kuma (Pod)  │◄─┼───┼──┤Blackbox Exporter│  │  │
│  │  │  (Deployment + PVC) │  │   │  └────────▲────────┘  │  │
│  │  └──────────┬──────────┘  │   │           │           │  │
│  │             │ TCP:3306    │   │  ┌────────┴────────┐  │  │
│  │  ┌──────────▼──────────┐  │   │  │   Prometheus    │  │  │
│  │  │    MariaDB (Pod)    │◄─┼───┼──┤ MySQLd Exporter │  │  │
│  │  │ (StatefulSet + PVC) │  │   │  └────────┬────────┘  │  │
│  │  └─────────────────────┘  │   │           │           │  │
│  └───────────────────────────┘   │  ┌────────▼────────┐  │  │
│                                  │  │     Grafana     │  │  │
│                                  │  └─────────────────┘  │  │
│                                  └───────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Слой 1: Продуктовое приложение (`uptime-kuma`)
* **Uptime Kuma (`Deployment`)** — движок проверок доступности сайтов и API. Настроены `readinessProbe` и `livenessProbe`, данные сохраняются на `PersistentVolumeClaim`.
* **MariaDB (`StatefulSet`)** — внешняя СУБД для хранения истории проверок, конфигураций мониторов и учётных записей. Использует отдельный `PVC` и `Secret` для реквизитов доступа.

### Слой 2: Стек наблюдаемости (`monitoring`)
* **Blackbox Exporter (`Deployment`)** — проверяет HTTP-доступность интерфейса Uptime Kuma снаружи по внутренней DNS-сети кластера (`http://uptime-kuma.uptime-kuma.svc.cluster.local:3001`).
* **MySQLd Exporter (`Deployment`)** — собирает внутренние метрики производительности и соединений MariaDB.
* **Prometheus (`Deployment` + `ConfigMap` + `PVC`)** — опрашивает экспортеры каждые 10 секунд и хранит временные ряды метрик.
* **Grafana (`Deployment` + `ConfigMap` + `PVC`)** — визуализирует состояние приложения, БД и HTTP-проб на дашбордах.

---

## Структура репозитория

```text
.
├── k8s/
│   ├── app/
│   │   └── manifest.yaml          # Namespace uptime-kuma, Secret, PVC, StatefulSet (MariaDB), Deployment (Kuma), Services
│   └── monitoring/
│       └── manifest.yaml          # Namespace monitoring, ConfigMaps, Secret, PVC, Prometheus, Grafana, Exporters
├── compose/                       # Модульные файлы Docker Compose (app, monitoring, network)
├── dagger/                        # Программируемый пайплайн на Dagger (деплой, API-настройка Grafana, Chaos Test)
└── README.md
```

---

## Быстрый старт в Kubernetes

### Предварительные требования
* Локальный или облачный Kubernetes-кластер (`Docker Desktop`, `Colima`, `Minikube`, `kind` или `k3d`)
* Установленный `kubectl`

### 1. Развёртывание стенда
Примените манифесты продуктового слоя и стека мониторинга:

```bash
kubectl apply -f k8s/app/manifest.yaml
kubectl apply -f k8s/monitoring/manifest.yaml
```

### 2. Проверка статуса подов и томов
Дождитесь перехода всех подов в статус `Running` (`1/1 Ready`):

```bash
kubectl get pods -n uptime-kuma
kubectl get pods -n monitoring
kubectl get pvc -A
```

### 3. Доступ к веб-интерфейсам (`port-forward`)
Пробросьте порты сервисов на локальную машину:

```bash
# Uptime Kuma -> http://localhost:3001
kubectl port-forward svc/uptime-kuma -n uptime-kuma 3001:3001 &

# Grafana -> http://localhost:3000
kubectl port-forward svc/grafana -n monitoring 3000:3000 &

# Prometheus -> http://localhost:9090
kubectl port-forward svc/prometheus -n monitoring 9090:9090 &
```

### 4. Проверка Self-Healing и сохранности данных
Вы можете принудительно удалить под приложения или базы данных — контроллеры `Deployment` / `StatefulSet` автоматически пересоздадут их, а данные останутся на `PVC`:

```bash
kubectl delete pod -l app=uptime-kuma -n uptime-kuma
kubectl get pods -n uptime-kuma -w
```

### 5. Остановка и очистка ресурсов

```bash
pkill -f "kubectl port-forward"
kubectl delete -f k8s/monitoring/manifest.yaml
kubectl delete -f k8s/app/manifest.yaml
```
*(Для полного удаления персистентных томов данных: `kubectl delete pvc --all -n uptime-kuma`).*

---
