# Use an official lightweight Python image.
FROM python:3.12-slim

# Set environment variables to prevent Python from buffering output and writing>
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory inside the container.
WORKDIR /app

# Install system dependencies needed for your application and pipenv.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the dependency files first (to leverage Docker caching).
COPY requirements.txt .

# Install Python dependencies.
