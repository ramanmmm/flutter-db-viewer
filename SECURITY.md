# Security policy

## Scope

flutterdb reads app data from a device you control and serves it on
`127.0.0.1`. Relevant issues include anything that could:

- expose that data beyond the local machine (e.g. binding to a public interface),
- execute unintended commands on the host or device (e.g. through a crafted
  database file name or table name),
- write to the device or the app's files.

## Reporting

Please **don't** open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/ramanmmm/flutter-db-viewer/security/advisories/new)
or email **mishraram929@gmail.com** with steps to reproduce. You'll get a reply
within a few days, and a fix or mitigation plan as soon as the issue is
confirmed.

## Supported versions

Only the latest release receives fixes.
