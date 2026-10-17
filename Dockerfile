FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# ffmpeg: downloads/HLS. xvfb+xauth: virtual display for pyvirtualdisplay. fonts: so pages render properly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg xvfb xauth fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers go to a SHARED path (not /root/.cache) so the unprivileged "nobody" user that
# runs source scripts can use them. --with-deps installs the system libraries Chromium needs.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# "chromium" = Playwright's bundled build; "chrome" = real Google Chrome (installed to /opt/google/chrome),
# needed by scripts that use channel="chrome". Google Chrome is amd64-only (no Linux arm64 build).
RUN playwright install --with-deps chromium \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then playwright install chrome; \
       else echo "WARNING: Google Chrome is not available on $(dpkg --print-architecture); channel='chrome' scripts will fail"; fi \
    && chmod -R a+rX /ms-playwright /opt 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/*

# patchright (patched Playwright for harder sites) has its OWN browser downloads; same shared path so "nobody" can use them.
RUN patchright install chromium \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then patchright install chrome; fi \
    && chmod -R a+rX /ms-playwright /opt 2>/dev/null || true

# Xvfb needs this socket dir to exist and be world-writable (sticky) when run as a non-root user.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY app ./app
COPY examples ./examples
COPY tests ./tests

# The API runs as root INSIDE the container on purpose: it drops every source script to the
# unprivileged user "nobody" (SCRIPT_RUN_AS_UID), so scripts cannot read /proc/1/environ,
# the secrets file, or the library folder.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
