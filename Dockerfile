FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1

# Static, not wired to $PORT: EXPOSE is documentation-only to Docker (it
# doesn't control what port the container actually binds to) - the real
# binding is entrypoint.sh's `uvicorn --port` plus docker-compose.yml's
# `ports:` mapping, both driven by $PORT at runtime. Parameterizing this
# with an ARG/ENV would add complexity with no functional effect, so it's
# kept static at the default.
EXPOSE 2012

ENTRYPOINT ["./entrypoint.sh"]
