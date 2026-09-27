# Security policy

## Reporting a vulnerability

Report security problems privately, not in a public issue. On GitHub, open
this repository's **Security** tab and choose **Report a vulnerability**. The
report stays private between you and the maintainers.

Say what you found, how to reproduce it, and which image tag or commit you
tested. Leave real keys, passwords, and camera URLs out; a made-up value shows
the problem just as well.

## What counts

The service holds a LunaRoute API key, an MQTT password, and camera URLs that
often carry credentials, and it sends camera frames to the LunaRoute gateway.
Report anything that could expose those secrets or frames, such as:

- a key, password, or camera URL showing up in logs, MQTT messages, Home
  Assistant attributes, error output, or the Docker image
- camera frames going anywhere other than the LunaRoute gateway

The README's [Limitations](README.md#limitations) section lists known gaps,
such as the unchecked camera certificates; those need no report.

Fixes land on `main` and in the image's `latest` tag.
