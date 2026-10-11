FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# 568 is TrueNAS's built-in "apps" user; override with `user:` in compose if needed.
ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    REPO_PATH=/data/repo \
    CACHE_PATH=/data/cache
RUN mkdir -p /data && chown 568:568 /data
USER 568:568
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

ENTRYPOINT ["tini", "--"]
# No --proxy-headers: the login treats the real TCP peer as the client address,
# so a forged X-Forwarded-For header can never make a request look local.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
