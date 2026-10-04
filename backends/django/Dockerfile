FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/django

# psycopg 的純 Python 驅動呼叫系統 libpq；只安裝執行所需函式庫。
RUN apt-get update && apt-get install -y --no-install-recommends libpq5=15.19-0+deb12u1 && rm -rf /var/lib/apt/lists/*

COPY backends/django/requirements.txt ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt

COPY backends/django/manage.py ./manage.py
COPY backends/django/site_api ./site_api

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000", "--noreload"]
