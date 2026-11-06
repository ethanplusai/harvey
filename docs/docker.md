# Running Sellbird with Docker (Linux)

Docker Compose runs the dashboard; start and stop the heartbeat from its
Actions card. Host networking preserves the dashboard's loopback-only bind at
`127.0.0.1:5555`. This Compose setup supports native Linux; use the pip install
above on macOS or Windows. On a remote Linux host, reach the dashboard through
an SSH tunnel rather than exposing its port to the network.

```bash
git clone https://github.com/ethanplusai/sellbird.git
cd sellbird

# Create the credentials file only if you do not already have one.
test -e .env || cp .env.example .env

# Run as your Linux user, never root. Set these again when rebuilding.
export SELLBIRD_UID=$(id -u) SELLBIRD_GID=$(id -g)

# Sign in to Claude on the host first; ~/.claude must already exist.
# The image ships Claude only; Codex is not included.
docker compose build
docker compose run --rm sellbird setup

docker compose up -d
docker compose logs sellbird
# Open the private unlock link printed in the logs.
```

Compose mounts the checkout directory at `/app`. Setup credentials, both YAML
config files, training output, knowledge files, and `data/` remain in that
checkout when containers are recreated. Directory mounting also permits atomic
configuration saves; do not replace it with individual config-file mounts.
Sellbird loads `.env` directly. Restart the container after editing credentials.
The checkout and Claude auth directory must be writable by the selected user.

To train later, use `docker compose run --rm sellbird train <product-url>`;
`sellbird.local.yaml` is saved directly to the checkout. Back up this file, `.env`,
`skills/`, and `data/` before moving installations. The running code also comes
from the checkout: restart after updating it, and rebuild when dependencies or
the Dockerfile change. The build context excludes local data and credentials.

The dashboard listens on 5555. If you change the port in the Compose `command`,
change the Dockerfile health-check port too and rebuild. Stop the service with
`docker compose down`; its files remain in the checkout. Use a host install if
you need the Codex provider.
