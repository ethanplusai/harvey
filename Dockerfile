FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN curl -fsSL https://claude.ai/install.sh | sh || true

# Install Playwright system deps
RUN pip install playwright && playwright install --with-deps chromium

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY harvey/ harvey/
COPY prompts/ prompts/
COPY harvey.yaml .

# Create data directory
RUN mkdir -p /app/data

# Run Harvey
CMD ["python", "-m", "harvey.main"]
