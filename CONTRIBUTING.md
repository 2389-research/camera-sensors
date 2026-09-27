# Contributing

Bug reports, questions, and pull requests are welcome.

## Reporting a bug

Open an issue that says:

- how you run the service: the image tag you pulled, or the commit you built
- what you expected, and what happened instead
- the log lines around the problem, which never hold keys or camera URLs
- your `config.yaml`, if it matters

`config.yaml` never holds a key or a password, but a camera URL written out
in full can still carry a secret: UniFi Protect puts a stream token in the
URL's path. Replace any such token before you paste the file. Never paste
your `.env`, an API key, a broker password, or a camera URL. If the bug
itself exposes one of those, report it privately instead, as
[SECURITY.md](SECURITY.md) describes.

## Changing the code

The README's [Contributing](README.md#contributing) section covers setup,
where things live, the checks, and how to propose a change. In short: add a
test that shows the change, keep the README and `docs/spec.md` true to the
code, and open a pull request once `scripts/check` ends with
`All checks passed.`

The project asks for no contributor license agreement and no sign-off.
