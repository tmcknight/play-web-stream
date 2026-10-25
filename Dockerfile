# Chromium and its shared libraries make this image large. They are needed for the
# fallback that handles players built at runtime.
# Pinned by digest because tags move, and a rebuild that differs next month cannot be
# reproduced. Bump the digest and the tag together, by hand.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    CHROMIUM_NO_SANDBOX=1 \
    PWS_PORT=8786

WORKDIR /app

COPY requirements.txt ./
# pyatv pulls in miniaudio, which has no wheels and needs a C++ compiler to build.
# The compiler is installed and removed in the same layer so it stays out of the
# image. Everything else installs from wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove g++ \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright \
 && rm -rf /var/lib/apt/lists/*

# The image has no .git for webapp.py to ask, so the build passes the version in.
# Declared after the dependency layer, so a new version does not rebuild it.
ARG PWS_VERSION=""
ENV PWS_VERSION=$PWS_VERSION

COPY hls_proxy.py resolve.py browser_find.py airplay.py airplay_protocol.py \
     webapp.py ui.html ./

# /config is created here and owned by app so an empty named volume mounted over it
# inherits that owner. Otherwise it is root-owned and pairing cannot save credentials.
RUN useradd --uid 10001 --create-home app \
 && mkdir -p /config \
 && chown app:app /config
USER app

# 8786 is the UI; each stream takes the next free port from 8787. 49170/udp is
# AirPlay's timing callback, which the receiver connects back on.
EXPOSE 8786 8787-8806 49170/udp

# Uses an opener with no proxy handlers instead of urlopen. If a deployment exports
# https_proxy, urlopen would send this 127.0.0.1 request down a tunnel that cannot
# return it, and a working container would report itself unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD \
  python -c "import os,urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:' + os.environ['PWS_PORT'] + '/api/streams', timeout=4)"

CMD ["python", "webapp.py"]
