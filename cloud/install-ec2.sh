#!/usr/bin/env bash
# Bootstrap Caddy + oauth2-proxy on the EC2 frontdoor.
#
# Run from the cloud/ directory on the EC2 box (Amazon Linux 2023, x86_64),
# after `git clone` of this repo and `sudo` access:
#
#   sudo ./install-ec2.sh
#
# Before running:
#   - example.com must already resolve to this EC2's EIP (Caddy uses
#     HTTP-01 to get the cert; the cert request fails otherwise).
#   - You must have a Google OAuth client (Web type) created at
#     https://console.cloud.google.com/apis/credentials with redirect URI
#     https://example.com/oauth2/callback. Drop client_id /
#     client_secret into /etc/oauth2-proxy/oauth2-proxy.env.
#   - The autossh tunnel from the Orin must be up (or this box won't have
#     anything to reverse-proxy to). Caddy will start anyway and serve 502
#     until the tunnel is alive.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (sudo)" >&2
    exit 1
fi

cd "$(dirname "$0")"

# --- caddy --------------------------------------------------------------
# tar / gzip / curl-minimal are already on AL2023; don't try to install
# full curl, it conflicts with curl-minimal.

CADDY_VERSION="2.8.4"
if [[ ! -x /usr/local/bin/caddy ]]; then
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
        -o "${tmp}/caddy.tar.gz"
    tar -C "${tmp}" -xzf "${tmp}/caddy.tar.gz" caddy
    install -m 0755 "${tmp}/caddy" /usr/local/bin/caddy
    rm -rf "${tmp}"
fi

id -u caddy &>/dev/null || \
    useradd --system --home-dir /var/lib/caddy --create-home --shell /sbin/nologin caddy

mkdir -p /etc/caddy /var/lib/caddy
install -m 0644 -o root -g root Caddyfile /etc/caddy/Caddyfile
chown -R caddy:caddy /var/lib/caddy

install -m 0644 caddy.service /etc/systemd/system/caddy.service

# --- oauth2-proxy -------------------------------------------------------

OAP_VERSION="7.6.0"
if [[ ! -x /usr/local/bin/oauth2-proxy ]]; then
    tmp=$(mktemp -d)
    curl -fsSL "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/v${OAP_VERSION}/oauth2-proxy-v${OAP_VERSION}.linux-amd64.tar.gz" \
        -o "${tmp}/oap.tar.gz"
    tar -C "${tmp}" -xzf "${tmp}/oap.tar.gz" --strip-components=1
    install -m 0755 "${tmp}/oauth2-proxy" /usr/local/bin/oauth2-proxy
    rm -rf "${tmp}"
fi

id -u oauth2-proxy &>/dev/null || \
    useradd --system --home-dir /var/lib/oauth2-proxy --create-home --shell /sbin/nologin oauth2-proxy

mkdir -p /etc/oauth2-proxy
install -m 0644 -o root -g root oauth2-proxy.cfg /etc/oauth2-proxy/oauth2-proxy.cfg

if [[ ! -f /etc/oauth2-proxy/oauth2-proxy.env ]]; then
    install -m 0600 -o oauth2-proxy -g oauth2-proxy oauth2-proxy.env.example /etc/oauth2-proxy/oauth2-proxy.env
    echo
    echo "  >>> EDIT /etc/oauth2-proxy/oauth2-proxy.env with the Google OAuth"
    echo "      client_id/secret and a cookie_secret, then re-run the systemctl"
    echo "      enable line at the bottom of this script."
    echo
fi

if [[ ! -f /etc/oauth2-proxy/emails ]]; then
    cat > /etc/oauth2-proxy/emails <<'EOF'
# One email per line — only Google accounts listed here will be allowed in.
EOF
    chmod 0644 /etc/oauth2-proxy/emails
    echo
    echo "  >>> ADD allowed emails to /etc/oauth2-proxy/emails (one per line)."
    echo
fi

install -m 0644 oauth2-proxy.service /etc/systemd/system/oauth2-proxy.service

# --- sshd keepalive -----------------------------------------------------
# Without this, an unclean Orin reboot orphans the reverse-tunnel forward on
# 127.0.0.1:8080 for ~2h and the public site is dead the whole time. See the
# header of sshd-keepalive.conf. Reload (not restart) so we can't drop the
# session running this script; sshd -t first so a bad file never reaches the
# running daemon.
if grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
    install -m 0600 -o root -g root sshd-keepalive.conf \
        /etc/ssh/sshd_config.d/10-belfry-keepalive.conf
    if sshd -t; then
        systemctl reload sshd
    else
        echo "  >>> sshd -t FAILED; not reloading. Fix before trusting the tunnel." >&2
    fi
else
    echo
    echo "  >>> /etc/ssh/sshd_config has no 'Include /etc/ssh/sshd_config.d/*.conf'."
    echo "      A drop-in would be ignored — append the ClientAlive lines from"
    echo "      cloud/sshd-keepalive.conf to the END of sshd_config by hand,"
    echo "      then: sudo sshd -t && sudo systemctl reload sshd"
    echo
fi

# --- enable -------------------------------------------------------------

systemctl daemon-reload
systemctl enable --now caddy oauth2-proxy

systemctl --no-pager --full status caddy oauth2-proxy | head -40 || true

echo
echo "Done. Verify:"
echo "  - /etc/oauth2-proxy/oauth2-proxy.env has client_id/secret/cookie_secret"
echo "  - /etc/oauth2-proxy/emails lists the two allowed users"
echo "  - sudo systemctl restart oauth2-proxy after editing those"
echo "  - https://example.com should redirect to Google login"
