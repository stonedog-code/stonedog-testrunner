Drop one file per test server here, named `<runner_id>.pub`, containing that
server's base64 public key — the one it prints on its first start.

This is the production enrolment path. The edge accepts a test server only if
the key it presents matches the file, and the enrolling server must sign its
own request with the key it presents, so nobody can register a key they do not
hold. That matters because the runner id is what result ownership is checked
against: squatting one would be enough to post forged results into your Slack
channel from inside your network.

The directory is mounted read-only. The edge never writes here.
