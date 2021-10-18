FROM python:3.9-alpine

# Install dependencies
RUN apk add --no-cache build-base libffi-dev git

# Prepare the application
COPY . /opt
RUN cd /opt && \
    pip3 install --upgrade pip setuptools && \
    pip3 install -r requirements.txt && \
    pip3 install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git

# Run the application
WORKDIR /opt
ENTRYPOINT ["python3", "netbox-sync.py"]
