FROM python:3.11-slim

WORKDIR /app

# Install build tools for Cython compilation (.so C-extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Compile sensitive Python files into C-binary .so files & purge plain source code
RUN python setup_cython.py build_ext --inplace && \
    rm -f bot.py client_manager.py database.py config.py *.c setup_cython.py && \
    apt-get purge -y gcc g++ python3-dev build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Run from compiled .so binaries
CMD ["python", "-u", "run.py"]
