# Mission Control in a container: one user's team in one sandbox.
# Build without a browser (small, for hosted fleets):
#   docker build -t missioncontrol .
# Build with the Playwright browser baked in (adds ~700MB):
#   docker build --build-arg WITH_BROWSER=1 -t missioncontrol:browser .
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      playwright install --with-deps chromium; \
    fi

COPY . .

# All user data (config, memory, skills, db) lives on this volume.
ENV MISSIONCONTROL_HOME=/data
VOLUME /data
EXPOSE 8899

# Remote clients must present MC_API_TOKEN (pass it as an env var).
CMD ["uvicorn", "main:app", "--app-dir", "server", "--host", "0.0.0.0", "--port", "8899"]
