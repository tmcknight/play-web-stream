# Chromium and its shared libraries are the only reason this image is large; they are
# needed for the fallback that handles players assembled at runtime.
# Pinned by digest: a tag moves, and an image that rebuilds differently next month
# is a deploy nobody can reproduce. Bump both of these deliberately.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    CHROMIUM_NO_SANDBOX=1 \
    PWS_PORT=8786

WORKDIR /app

COPY requirements.txt ./
# pyatv pulls miniaudio, which publishes no wheels and needs a C++ compiler. It is
# wanted only to build, so it is installed and removed inside one layer rather than
# left in the image. Everything else here resolves to a wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove g++ \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright \
 && rm -rf /var/lib/apt/lists/*

COPY hls_proxy.py resolve.py browser_find.py airplay.py airplay_protocol.py \
     webapp.py ui.html ./

# /config is created here, owned by app, so that an empty named volume mounted over it
# inherits that ownership -- otherwise it arrives root-owned and pairing cannot write
# the credentials it just obtained.
RUN useradd --uid 10001 --create-home app \
 && mkdir -p /config \
 && chown app:app /config
USER app

# 8786 is the UI; each stream takes the next free port from 8787. 49170/udp is
# AirPlay's timing callback, which the receiver dials back on.
EXPOSE 8786 8787-8806 49170/udp

# Built with the proxy handlers emptied rather than with urlopen: a deployment that
# exports https_proxy would otherwise send this at 127.0.0.1 through a tunnel that
# cannot bring it back, and the container would report itself unhealthy while serving.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD \
  python -c "import os,urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:' + os.environ['PWS_PORT'] + '/api/streams', timeout=4)"

CMD ["python", "webapp.py"]
