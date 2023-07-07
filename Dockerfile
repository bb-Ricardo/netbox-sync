FROM python:3.9-slim-bookworm AS builder

COPY requirements.txt .

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m venv /opt/netbox-sync/venv && \
    /opt/netbox-sync/venv/bin/python3 -m pip install --upgrade pip && \
    /opt/netbox-sync/venv/bin/pip install -r requirements.txt && \
    /opt/netbox-sync/venv/bin/pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git

FROM python:3.9-slim-bookworm AS netbox-sync

# Copy installed packages
COPY --from=builder /opt/netbox-sync/venv /opt/netbox-sync/venv

# Add netbox-sync user
RUN groupadd --gid 1000 netbox-sync && \
    useradd --uid 1000 --gid netbox-sync --shell /bin/sh \
    --no-create-home --system netbox-sync

USER netbox-sync

# Prepare the application
WORKDIR /app
COPY --chown=netbox-sync:netbox-sync . .

# Use virtual env packages and allow timezone setup
ENV PATH=/opt/netbox-sync/venv/bin:$PATH
ENV TZ=Europe/Berlin

ENTRYPOINT ["python3", "netbox-sync.py"]
