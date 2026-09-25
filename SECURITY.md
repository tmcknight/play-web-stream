# Security

## What this is meant to be reachable from

A LAN. Nothing else. The web app binds all interfaces so that phones and Apple TVs can
reach it, and refuses any client outside private address space — but that check is a
guard rail, not a perimeter. There is no authentication in front of it, by design: the
network it sits on *is* the authentication.

So do not give it a public hostname through nginx-proxy-manager, a Cloudflare tunnel, or
a port forward. Anyone who reaches it can make the machine fetch arbitrary URLs and
re-serve the bytes, which is an open relay for third-party video attributable to your
address and your domain. Sustained video through a Cloudflare tunnel is against their
terms besides.

The `PWS_ALLOW_ANY=1` escape hatch exists so the refusal can be lifted on a network the
operator understands better than the check does. It is not a supported deployment.

## The name you reach it by

The client's address establishes which network it is on. It does not establish which
*page* is driving that client, and those are different questions: a site can point its
own hostname at this app's LAN address, and a browser in the house will then send it
requests that pass every address check here — and read the replies, because the browser
still believes it is talking to that site. The phone in the room is on the advertised
`/24`, so that route reaches the AirPlay controls as well.

So the `Host` header has to be one this app answers to. An address always is: there is
no name for anyone else's DNS to repoint. `localhost` always is, because that is how the
box's own browser and its health check arrive. A hostname is only if `PWS_ALLOW_HOSTS`
names it — and the
rule for that list is that you serve its DNS yourself, on your router, Pi-hole, Unbound
or mDNS. A name resolved by somebody else is a name somebody else can move.

Reach it by address or by QR and the list stays empty. Reach it at
`nas.local` or `pws.lan` and that name goes in the list, or the app answers 403 and
says which variable to set.

Requests a browser marks `Sec-Fetch-Site: cross-site` are refused on the same grounds.
`GET /api/resolve` is a "simple" request that CORS lets another site issue without
asking first; it cannot read the reply, but it can make this app go and fetch something,
which is enough.

`PWS_ALLOW_ANY=1` lifts the `Host` check along with the address one, since both are
guard rails around the same assumption. It does not lift the cross-site refusal, which
is about a different thing entirely.

## What the container is holding

Chromium runs with its own sandbox off, because switching it on means giving the
container back the user namespaces Docker's seccomp profile withholds — loosening the
boundary to tighten what sits inside it. The compose files go the other way: no
capabilities, `no-new-privileges`, a read-only root with tmpfs for `/tmp` and `$HOME`,
and a pid cap. The process already runs as an unprivileged user with nothing on the
image to escalate to.

That boundary is load-bearing, because the fallback resolver drives a real browser over
whatever page it is pointed at.

Installed with `deploy/install.sh` there is no container, and the systemd unit carries
the same posture by the other mechanism: no new privileges, no capabilities, a
read-only filesystem, your home readable but not writable, and one path held open for
the AirPlay credentials. `systemd-analyze security play-web-stream` scores it. Three
directives that look like they belong there are deliberately absent, and the unit says
why beside each -- the short version is that a browser is not a normal service, and
that the proxies outlive a restart on purpose.

## What is already held tighter than the rest

**The AirPlay hand-off** is server-initiated — the app, holding paired credentials on the
household LAN, starts playback on a television in the house — so a viewer who is not in
the house pressing it would be borrowing the server's network position. Anything
forwarded to the app arrives from loopback and so passes the private-address check like
anyone else, which is why that route asks for a stronger signal instead: the client has
to sit in the same `/24` as the address the proxies advertise. Loopback is refused with
everything else off that subnet.
`GET /api/airplay` and `GET /api/receivers` decline to name the televisions in the house
to such a client rather than merely refusing to act, and the three POST routes answer
403. Pairing is held to the same bar as playback, because it writes credentials.

**The URLs a proxy hands out.** Each proxy signs its playlist and segment URIs with a key
that lives and dies with the process, so whoever holds a stream's path token can fetch
that stream and nothing else. A URL forged for another host on the network gets a 404.
The token is the only credential on a running proxy — there is no check on who is asking
— so treat a playback URL like a password, and stop the stream when nobody is watching.

**Paired AirPlay credentials** are written to `/config/pyatv.conf` in the container, or
`~/.config/play-web-stream/pyatv.conf` from a checkout. They are not in the repo and must
not be committed.

## What the egress proxy does and does not hide

With `PWS_EGRESS_PROXY` set, every upstream fetch — playlist, segments, header probe, and
the headless browser's page load — leaves through it, so a stream origin sees the exit
node rather than this connection. What that is worth is bounded in ways worth stating:

- **Only the origin's view is changed.** The app is still on the LAN, still serves the
  LAN directly, and is still reachable exactly as before. Nothing about the tunnel makes
  it safer to expose.
- **The proxy credentials are configuration, not a secret this holds carefully.** They
  sit in the environment, are visible to anything that can read it, and are printed
  nowhere: every log line and API answer carries the proxy with its credentials stripped.
- **A LAN destination is never sent through it**, by address rather than by name — so a
  hostname that happens to resolve to a private address would be proxied. Upstream media
  is named and addressed publicly, so that is a fetch nothing here makes, but it is the
  edge of the rule rather than an oversight.
- **A tunnel that stops fails closed.** Fetches go through the proxy or they error; there
  is no fallback to a direct connection anywhere in the pipeline, and `GET /api/egress`
  reports the failure rather than answering from this address.
- **`PWS_FORCE_PROXY=0` reopens it.** A stream that needs no rewriting is then handed to
  the player as the origin's own URL, and the player — or the Apple TV holding that URL —
  fetches the origin from this network. It defaults on for that reason.

## What is deliberately not defended

The proxy fetches whatever URL it is told to, with whatever `Referer` the resolver worked
out the origin wanted. That is the entire product, so it is not treated as a
server-side request forgery bug. It is the reason the LAN boundary matters.

## Scope

This is a personal project maintained on a best-effort basis, with no release cadence and
no supported versions table: the fix, if there is one, lands on `main`.

## Reporting

Open a [security advisory](https://github.com/tmcknight/play-web-stream/security/advisories/new)
rather than a public issue for anything that lets a client reach past the boundaries
above. For everything else an ordinary issue is fine.
