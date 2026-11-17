FROM python:3.15-alpine

LABEL maintainer="Mason Parker <masonparker868@gmail.com>"
LABEL version="1.0.0"
LABEL description="qbt with natpmpc"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_COLORS=0 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Add required packages
RUN apk update && apk upgrade --no-cache && \
    apk add --no-cache \
    libnatpmp

# Copy application files and use pip to install python dependencies from pyproject.toml
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Copy entrypoint and make executable
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ensure entrypoint's line endings are Unix
RUN sed -i 's/\r$//g' /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]