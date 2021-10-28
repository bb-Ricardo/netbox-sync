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
ENTRYPOINT ["python3", "netbox-sync.py"]
