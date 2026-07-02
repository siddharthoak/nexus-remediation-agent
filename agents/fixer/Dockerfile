# Pinned to Debian bookworm explicitly: the rolling "3.11-slim" tag has moved to a newer
# Debian base that no longer ships an openjdk-17 package (only 21/25). Pin here so this
# build doesn't silently break the next time the upstream base image rolls forward.
FROM python:3.11-slim-bookworm

WORKDIR /app

# Multi-runtime image: the Fixer's frameworks/ package detects the target repo's build
# system at runtime (Maven, Gradle, npm, or Python) and shells out to the matching tool.
# All four runtimes are installed here rather than shipping per-framework images — see
# PLAN.md section 4.7 for the tradeoff (larger image, single Dockerfile to maintain).
# Gradle itself is not installed: repos provide their own ./gradlew wrapper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl ca-certificates gnupg \
        openjdk-17-jdk-headless \
        maven \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Build-time smoke test — fail the image build here, not at Fixer runtime, if a
# runtime is missing.
RUN java -version && mvn --version && node --version && npm --version && python3 --version

COPY agents/fixer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/common/ /app/common/
COPY agents/knowledge_agent/ /app/knowledge_agent/
COPY agents/classifier/ /app/classifier/
COPY agents/fixer/ /app/

CMD ["python", "main.py"]
