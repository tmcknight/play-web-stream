# Security

## What this is meant to be reachable from

A LAN only. The web app binds all interfaces for phones and Apple TVs, and refuses
clients outside private address space. That check is a safeguard, not a perimeter: there
is no authentication, so the network is the only protection.

Don't expose it through nginx-proxy-manager, a Cloudflare tunnel or a port forward.
Anyone who reaches it can make the machine fetch any URL and re-serve the bytes: an open
relay for third-party video, traced to your address and domain. Sustained video through
a Cloudflare tunnel also breaks their terms.

`PWS_ALLOW_ANY=1` lifts the refusal for an operator who knows their network better than
the check does. It is not a supported deployment.

## The name you reach it by

A client's address shows its network, not which *page* is driving it. A site can point
its own hostname at this app's LAN address; a browser in the house then sends requests
that pass every address check, and reads the replies, because it thinks it is talking to
that site. The phone in the room is on the advertised `/24`, so this also reaches the
AirPlay controls.

So the `Host` header must be one the app answers to:

- An IP address always is: there is no name for someone else's DNS to repoint.
- `localhost` always is, for the machine's own browser and health check.
- A hostname only if `PWS_ALLOW_HOSTS` lists it. List only names whose DNS you serve
  (router, Pi-hole, Unbound or mDNS), since someone else's DNS can move a name.

Reach it by address or QR code and the list stays empty. Reach it at `nas.local` or
`pws.lan` and add that name, or the app answers 403 and names the variable to set.

Requests marked `Sec-Fetch-Site: cross-site` are refused for the same reason.
`GET /api/resolve` is a CORS "simple" request that another site can send without a
preflight. It can't read the reply, but it can make this app fetch something.

`PWS_ALLOW_ANY=1` lifts the `Host` check too, as both rest on the same assumption. It
doesn't lift the cross-site refusal, which guards against something else.

## What the container is holding

Chromium's own sandbox is off. Turning it on needs the user namespaces Docker's seccomp
profile withholds, which would loosen the container to tighten the browser. The compose
files tighten the container instead: no capabilities, `no-new-privileges`, a read-only
root with tmpfs for `/tmp` and `$HOME`, and a pid limit. The process runs unprivileged,
with nothing on the image to escalate to. This matters because the fallback resolver
runs a real browser on whatever page it is given.

`deploy/install.sh` has no container; its systemd unit applies the same limits: no new
privileges, no capabilities, a read-only filesystem, home readable but not writable, and
one writable path for AirPlay credentials. `systemd-analyze security play-web-stream`
scores it. Three directives you might expect are left out, with reasons in the unit: a
browser is not a normal service, and the proxies must outlive a restart.

## What is already held tighter than the rest

**The AirPlay hand-off** is started by the server, which holds paired credentials and
starts playback on a television in the house. A remote viewer pressing it would borrow
the server's network position. Forwarded requests arrive from loopback and pass the
private-address check, so this route requires the client to be in the advertised `/24`.
Loopback is refused too. Such clients get no list of televisions from `GET /api/airplay`
or `GET /api/receivers`, and 403 from the three POST routes. Pairing follows the same
rule, because it writes credentials.

**Proxy URLs.** Each proxy signs its playlist and segment URIs with a key that lasts only
as long as the process. A stream's path token fetches that stream and nothing else; a URL
forged for another host gets a 404. The token is the only credential on a running proxy,
so treat a playback URL like a password, and stop streams nobody is watching.

**Paired AirPlay credentials** live in `/config/pyatv.conf` in the container, or
`~/.config/play-web-stream/pyatv.conf` from a checkout. They are not in the repo and must
not be committed.

## What the egress proxy does and does not hide

With `PWS_EGRESS_PROXY` set, every upstream fetch (playlist, segments, header probe, the
headless browser's page load) goes through it, so origins see the exit node. Limits:

- **Only the origin's view changes.** The app still serves the LAN directly and is
  reachable as before. The tunnel doesn't make it safer to expose.
- **Proxy credentials are plain configuration.** They sit in the environment, readable by
  anything that can read it, and are never printed: logs and API responses strip them.
- **LAN destinations bypass it by address, not name.** A hostname resolving to a private
  address would be proxied. Upstream media uses public names and addresses, so nothing
  here makes such a fetch.
- **A stopped tunnel fails closed.** Fetches go through the proxy or error; nothing falls
  back to a direct connection, and `GET /api/egress` reports the failure.
- **`PWS_FORCE_PROXY=0` reopens the leak.** A stream needing no rewriting is handed out as
  the origin's own URL, which the player or Apple TV fetches from your network. That is
  why it defaults on.

## What is not defended

The proxy fetches whatever URL it is given, with whatever `Referer` the resolver decided
the origin wants. That is the product, so it is not treated as a server-side request
forgery bug. It is why the LAN boundary matters.

## Scope

A personal project, maintained on a best-effort basis. No release schedule and no
supported versions table. Fixes land on `main`.

## Reporting

Open a [security advisory](https://github.com/tmcknight/play-web-stream/security/advisories/new),
not a public issue, for anything that lets a client get past the boundaries above.
Anything else can be an ordinary issue.
