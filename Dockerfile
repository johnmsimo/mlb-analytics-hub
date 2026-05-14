# Stage 1: Build the Application
# We use python:3.11-slim as the base for building and installing dependencies.
FROM python:3.11-slim AS build

# Set the working directory inside the container
WORKDIR /usr/src/app

# Install system dependencies needed for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements.txt if it exists (using wildcard to avoid build failure)
COPY requirements.tx[t] ./requirements.txt

# Install Python dependencies only if requirements.txt exists
RUN pip install --upgrade pip && \
    if [ -f requirements.txt ]; then \
        pip install -r requirements.txt; \
    fi

# Copy the rest of the application source code
COPY . .

# Stage 2: Create the Final Production Image
# We use python:3.11-slim as a minimal runtime image.
FROM python:3.11-slim

# Set the working directory
WORKDIR /usr/src/app

# Install only runtime dependencies if needed
# redis-server is installed here so the container can run its own local Redis
# instance when no external REDIS_URL is supplied.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        redis-server \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from the build stage
COPY --from=build /opt/venv /opt/venv

# Copy the application code
COPY --from=build /usr/src/app .

# Set the virtual environment as the active Python environment
ENV PATH="/opt/venv/bin:$PATH"

# Create a non-root user to run the application
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /usr/src/app
USER appuser

# Expose the port your app runs on
ENV PORT=8080
EXPOSE $PORT

# Point the app at the local Redis instance.
# Override with a real REDIS_URL (e.g. Upstash, Redis Cloud) in production
# and the embedded redis-server is simply unused — redis_client.py will
# connect to the external URL instead.
ENV REDIS_URL=redis://localhost:6379

# Start Redis, wait until it is actually ready to accept connections (not just
# daemonised), then start the app.  Without the readiness loop the app can
# reach redis_client.py before Redis has bound its socket and permanently fall
# back to in-memory mode for the container lifetime.
CMD redis-server --daemonize yes \
        --dir /tmp \
        --save "" \
        --loglevel warning \
    && until redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.1; done \
    && python app.py
