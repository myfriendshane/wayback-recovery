# Use a minimal Python 3.11 base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the recovery script
COPY scripts/wayback_recover.py scripts/wayback_recover.py

# Default command: show help
CMD ["python", "scripts/wayback_recover.py", "--help"]
