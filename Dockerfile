FROM python:3.9-slim-bullseye

# Install dependencies
RUN apt-get update && apt-get -y upgrade && apt-get install -y git-core

# Prepare the application
COPY . /opt
RUN cd /opt && \
    pip3 install --upgrade pip setuptools && \
    pip3 install -r requirements.txt && \
    pip3 install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git

# Run the application
WORKDIR /opt

RUN set -eux; \
  addgroup --gid 1000 netbox-sync; \
  adduser --uid 1000 --ingroup netbox-sync --shell /bin/sh --home /home/netbox-sync --disabled-password netbox-sync

RUN chown -R netbox-sync:netbox-sync /opt

USER netbox-sync

ENTRYPOINT ["python3", "netbox-sync.py"]
