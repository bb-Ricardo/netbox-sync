FROM python:3.9-slim-bullseye

# Install dependencies
RUN apt-get update && apt-get -y upgrade && apt-get install -y git-core

# Run the application
WORKDIR /app

RUN set -eux; \
  addgroup --gid 1000 netbox-sync; \
  adduser --uid 1000 --ingroup netbox-sync --shell /bin/sh --home /home/netbox-sync --disabled-password \
          --gecos "netbox-sync,0815,2342,9001" netbox-sync

# Prepare the application
COPY Dockerfile LICENSE.txt netbox-sync.py README.md requirements.txt settings-example.ini /app/
COPY module /app/module

RUN chown -R netbox-sync:netbox-sync /app

# disable upgrading setup tools due to bug in setuptools and automation sdk
# once this is fixed, switch back to: pip3 install --upgrade pip setuptools
RUN cd /app && \
    pip3 install --upgrade pip && \
    pip3 install --force-reinstall 'setuptools==60.10.0' && \
    pip3 install -r requirements.txt && \
    pip3 install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git

USER netbox-sync

ENTRYPOINT ["python3", "netbox-sync.py"]
