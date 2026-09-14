# Деплой на Yandex Cloud

Цель — доступность продукта из России без зависимости от западных PaaS
(Streamlit Community Cloud и т.п.), где геодоступность для российских
пользователей не гарантирована. Решение: обычная Compute Cloud VM +
Docker Compose — то же самое, что и локальный запуск, просто на сервере.

## 1. Создать виртуальную машину

Yandex Cloud Console → Compute Cloud → «Создать ВМ»:

- Образ: Ubuntu 22.04 LTS
- Платформа: Intel Ice Lake, 2 vCPU / 2-4 GB RAM достаточно (пайплайн не
  тяжёлый: async-запросы к двум API раз в несколько часов)
- Диск: 20 GB HDD/SSD — с запасом на SQLite-файл и кэши
- Публичный IP: да
- Группа безопасности: разрешить входящий TCP 22 (SSH) и 8501 (дашборд)
  со своего IP или 0.0.0.0/0 на первое время

## 2. Установить Docker

```bash
ssh <user>@<vm_ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
docker compose version   # проверка, что плагин compose есть
```

## 3. Развернуть приложение

```bash
git clone <URL_репозитория>
cd Hello-git
cp .env.example .env    # при необходимости поправить интервал пайплайна
docker compose -f docker/docker-compose.yml up -d --build
```

Проверить:

```bash
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f scheduler   # первый прогон пайплайна
```

Дашборд: `http://<vm_ip>:8501`

## 4. (Рекомендуется) TLS + домен

Для постоянного продукта — поставить перед Streamlit reverse-proxy
(Nginx/Caddy) с Let's Encrypt и закрыть порт 8501 наружу, оставив 443:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
# nginx: proxy_pass http://127.0.0.1:8501; + upgrade-заголовки для WebSocket
# (Streamlit использует WebSocket для live-обновлений UI)
sudo certbot --nginx -d your-domain.example
```

В группе безопасности Yandex Cloud после этого закрыть 8501 для внешнего
трафика, оставить только 443/80 и 22.

## 5. Резервное копирование

Данные лежат в именованном Docker volume `kzbonds_data` (SQLite-файл +
кэши JSON). Бэкап:

```bash
docker run --rm -v kzbonds_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/kzbonds_data_$(date +%Y%m%d).tar.gz -C /data .
```

Складывать архивы в Yandex Object Storage (S3-совместимый) по cron на
хосте, если нужна история за пределами диска ВМ.

## 6. Проверка доступности из России

Обе биржи (KASE, AIX) — казахстанские, санкционных блокировок для
российских IP на момент написания не выявлено. Тем не менее:

- Перед боевым запуском выполните `docker compose logs scheduler` и
  убедитесь, что первый прогон пайплайна реально получил данные
  (`n_bonds > 0` в логе), а не упал по таймауту.
- AIX API — недокументированный внутренний эндпоинт фронтенда без SLA;
  при постоянных сбоях с одного облачного IP-диапазона (маловероятно, но
  возможно при выборочной защите от ботов) — рассмотреть NAT через
  другую зону/провайдера или добавить `User-Agent`/паузы между запросами
  в `kzbonds/sources/aix.py`.
