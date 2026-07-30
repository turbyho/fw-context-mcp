FROM python:3.11-slim

# libclang for C/C++ parsing (required by the pip libclang package)
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends wget gnupg && \
    echo "deb http://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-18 main" \
        >> /etc/apt/sources.list.d/llvm.list && \
    wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key | \
    gpg --dearmor -o /etc/apt/trusted.gpg.d/llvm.gpg && \
    apt-get update -y && \
    apt-get install -y --no-install-recommends libclang-18-dev && \
    apt-get purge -y wget gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY README.md pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir .

CMD ["fw-context-mcp"]
