FROM python:3.9-alpine

# Install dependencies
RUN apk add --no-cache build-base libffi-dev

# Prepare the application
COPY . /opt
RUN cd /opt && pip3 install -r requirements.txt

# Run the application
WORKDIR /opt
ENTRYPOINT ["python3", "netbox-sync.py"]
