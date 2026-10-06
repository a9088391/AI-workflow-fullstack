FROM python:3.13.16-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/fastapi

# psycopg 純 Python 驅動只需要執行期 libpq。
RUN apt-get update && apt-get install -y --no-install-recommends libpq5=15.19-0+deb12u1 && rm -rf /var/lib/apt/lists/*

COPY backends/fastapi/requirements.txt ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt

COPY backends/fastapi/site_api ./site_api

CMD ["uvicorn", "site_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
