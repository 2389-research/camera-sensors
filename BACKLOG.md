# Backlog

Known gaps, open to anyone who wants one. Open an issue before you start, so
nobody does the same work twice.

## Decide whether one model timeout should mark a sensor unavailable

Today any model failure, a timeout included, marks the sensor unavailable
until its next successful look (spec section 19). During a slow spell on the
gateway, that makes a sensor flicker between unavailable and its last state
in Home Assistant; `gotchas.md` records one such spell. One option is to wait
for several failures in a row before marking the sensor unavailable.

## Choose a TLS policy before PyAV bundles libavformat 63

FFmpeg starts checking TLS certificates by default at libavformat 63. Once a
PyAV release bundles it, an `rtsps://` camera whose certificate does not
chain to a trusted CA stops connecting. Before merging that `av` bump,
decide between passing `tls_verify=0` to keep today's behavior and adding a
config option for a CA file so that cameras can be checked. `gotchas.md` has
the details.
