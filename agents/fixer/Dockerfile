FROM python:3.11-slim

WORKDIR /app

# Install git (required by GitPython for clone/push operations)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY agents/fixer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/common/ /app/common/
COPY agents/fixer/ /app/

CMD ["python", "main.py"]
