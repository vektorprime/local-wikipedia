# Base image
FROM ubuntu:24.04

# Set the working directory
WORKDIR /app

ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

# Install Groonga, PGroonga, and PostgreSQL
RUN apt-get update && apt-get install -y \
    ca-certificates \
    lsb-release \
    wget && \
    wget https://packages.groonga.org/ubuntu/groonga-apt-source-latest-$(lsb_release --codename --short).deb && \
    apt install -y ./groonga-apt-source-latest-$(lsb_release --codename --short).deb && \
    apt-get update && \
    apt-get install -y \
    groonga \
    groonga-tokenizer-mecab \
    postgresql-16-pgroonga && \
    apt-get clean && rm -rf /var/lib/apt/lists/* ./groonga-apt-source-latest-*.deb

# Install Python
RUN apt-get update && apt-get install -Vy \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Create a Python virtual environment and add it to PATH
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH=/app/src

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    datasets \
    huggingface_hub \
    psycopg2-binary \
    mcp \
    pyyaml \
    starlette \
    uvicorn

# Install other assorted tools
RUN apt-get update && apt-get install -y \
    curl \
    sudo && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# sudo is required
# curl is not strictly required, but handy for debugging

# Copy each file
COPY src /app/src
# Copy the config file (default configuration)
COPY config.yaml .

ENTRYPOINT ["src/start.sh"]
# Temporary entrypoint
# ENTRYPOINT ["tail", "-f", "/dev/null"]
