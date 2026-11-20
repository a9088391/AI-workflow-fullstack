FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/flask

# psycopg 純 Python 驅動使用系統 libpq；只安裝執行期函式庫。
RUN apt-get update && apt-get install -y --no-install-recommends libpq5=15.20-0+deb12u1 && rm -rf /var/lib/apt/lists/*

COPY backends/flask/requirements.txt ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt

COPY backends/flask/run.py ./run.py
COPY backends/flask/site_api ./site_api

CMD ["python", "run.py"]
