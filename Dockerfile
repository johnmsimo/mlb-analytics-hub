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

# Install only runtime libraries required by Python dependencies.
# Redis is an external optional service; redis_client.py falls back to an
# in-process TTL cache when REDIS_URL is not configured.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from the build stage
COPY --from=build /opt/venv /opt/venv

# Copy the application code
COPY --from=build /usr/src/app .

# Set the virtual environment as the active Python environment
ENV PATH="/opt/venv/bin:$PATH"

# Build-time commit marker (passed by the deploy workflow), surfaced by /health
# so "is this commit actually live?" is a one-second check.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

# Create a non-root user to run the application
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /usr/src/app
USER appuser

# Expose the port your app runs on
ENV PORT=8080
EXPOSE $PORT

# Run the production WSGI server. The exec form preserves Fly.io shutdown
# signals, and gunicorn_conf.py owns worker/thread/time-out configuration plus
# the post-fork cache preload hook. wsgi.py installs Phase 4.26 confidence
# enrichment before exposing the Flask application.
CMD ["gunicorn", "--config", "gunicorn_conf.py", "wsgi:app"]
