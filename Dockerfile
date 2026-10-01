FROM python:3.14-slim AS builder

COPY requirements.txt .

ARG VENV=/opt/netbox-sync/venv

# Install dependencies
RUN python3 -m venv $VENV && \
    $VENV/bin/python3 -m pip install --upgrade pip && \
    $VENV/bin/pip install -r requirements.txt && \
    $VENV/bin/pip install vmware-vcenter==9.1.1.0 && \
    find $VENV -type d -name "__pycache__" -print0 | xargs -0 -n1 rm -rf

FROM python:3.14-slim AS netbox-sync

ARG VENV=/opt/netbox-sync/venv

# Install the security updates published since the base image was built
RUN apt-get update && \
    apt-get dist-upgrade -y && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages
COPY --from=builder $VENV $VENV

# Add netbox-sync user
RUN groupadd --gid 1000 netbox-sync && \
    useradd --uid 1000 --gid netbox-sync --shell /bin/sh \
    --no-create-home --system netbox-sync

# Prepare the application: the code belongs to root and is read-only for the
# service user; only the cache directory is writable (group 0 as well, so an
# arbitrary uid in group 0 can use it)
WORKDIR /app
COPY . .
RUN mkdir -p /app/cache && chown netbox-sync:0 /app/cache && chmod 0770 /app/cache

USER netbox-sync

# Use virtual env packages and allow timezone setup
ENV PATH=$VENV/bin:$PATH
ENV TZ=Europe/Berlin

ENTRYPOINT ["python3", "netbox-sync.py"]
