FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements file first
COPY requirements.txt .

# Install python packages
RUN pip install --no-cache-dir -r requirements.txt

# Install chromium browser and its OS-level dependencies
RUN playwright install --with-deps chromium

# Copy application files
COPY app.py index.html witree_logo.png ./

# Create images folder
RUN mkdir -p images && chmod 777 images

# Environment variables
ENV PORT=5000
ENV HARVEST_HOST=0.0.0.0
ENV HARVEST_DEV=false

# Expose port
EXPOSE 5000

# Start server
CMD ["python", "app.py"]
