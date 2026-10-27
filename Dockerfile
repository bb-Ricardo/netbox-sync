FROM python:3.14-slim AS builder

COPY requirements.txt .

ARG VENV=/opt/netbox-sync/venv
ARG INSTALL_OVIRT=false
COPY requirements-ovirt.txt .

# Install dependencies
RUN if [ "$INSTALL_OVIRT" = "true" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends gcc libc6-dev libxml2-dev libcurl4-openssl-dev && \
        rm -rf /var/lib/apt/lists/*; \
    fi && \
    python3 -m venv $VENV && \
    $VENV/bin/python3 -m pip install --upgrade pip && \
    $VENV/bin/pip install -r requirements.txt && \
    if [ "$INSTALL_OVIRT" = "true" ]; then $VENV/bin/pip install -r requirements-ovirt.txt; fi && \
    $VENV/bin/pip install vmware-vcenter==9.1.1.0 && \
    $VENV/bin/python3 -m pip uninstall -y pip && \
    find $VENV -type d -name "__pycache__" -print0 | xargs -0 -n1 rm -rf

FROM python:3.14-slim AS netbox-sync

ARG VENV=/opt/netbox-sync/venv
ARG INSTALL_OVIRT=false

# Copy installed packages
COPY --from=builder $VENV $VENV

# Copy application files
WORKDIR /app
COPY . .

# Install the security updates published since the base image was built,
# drop pip (not needed at runtime) and add the netbox-sync user.
# The code belongs to root and is read-only for the service user; only the
# cache directory is writable (group 0 as well, so an arbitrary uid in group 0
# can use it)
RUN apt-get update && \
    apt-get dist-upgrade -y && \
    if [ "$INSTALL_OVIRT" = "true" ]; then \
        apt-get install -y --no-install-recommends libxml2 libcurl4t64; \
    fi && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip uninstall -y --root-user-action=ignore pip && \
    groupadd --gid 1000 netbox-sync && \
    useradd --uid 1000 --gid netbox-sync --shell /bin/sh --no-create-home --system netbox-sync && \
    mkdir -p /app/cache && chown netbox-sync:0 /app/cache && chmod 0770 /app/cache

USER netbox-sync

# Use virtual env packages and allow timezone setup
ENV PATH=$VENV/bin:$PATH
ENV TZ=Europe/Berlin

ENTRYPOINT ["python3", "netbox-sync.py"]
