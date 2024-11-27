FROM python:3.11-slim-bookworm AS builder

COPY requirements.txt .

ARG VENV=/opt/netbox-sync/venv

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m venv $VENV && \
    $VENV/bin/python3 -m pip install --upgrade pip && \
    $VENV/bin/pip install -r requirements.txt && \
    $VENV/bin/pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git && \
    find $VENV -type d -name "__pycache__" -print0 | xargs -0 -n1 rm -rf

FROM python:3.11-slim-bookworm AS netbox-sync

ARG VENV=/opt/netbox-sync/venv

# Copy installed packages
COPY --from=builder $VENV $VENV

# Add netbox-sync user
RUN groupadd --gid 1000 netbox-sync && \
    useradd --uid 1000 --gid netbox-sync --shell /bin/sh \
    --no-create-home --system netbox-sync

USER netbox-sync

# Prepare the application
WORKDIR /app
COPY --chown=netbox-sync:netbox-sync . .

# Use virtual env packages and allow timezone setup
ENV PATH=$VENV/bin:$PATH
ENV TZ=Europe/Berlin

ENTRYPOINT ["python3", "netbox-sync.py"]
