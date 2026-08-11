#!/usr/bin/env bash
# check-ipv6.sh — prove IPv6 ingress works BEFORE publishing an AAAA record.
#
# Why the order matters: Let's Encrypt resolves AAAA first and, if the HTTP-01
# challenge fails over IPv6, does NOT fall back to IPv4. Publish AAAA for a
# host whose v6 ingress is closed and you get no certificate at all — then
# Caddy backs off with increasing delays, which reads as "flaky" rather than
# "misconfigured" and is miserable to debug.
#
# So: run this on the VPS, do the one external test it prints, and only then
# add the AAAA record.
#
# WHERE TO RUN THIS: on the VPS, over SSH, as root.
#
#   ssh root@179.237.107.22
#   cd /opt/ats-mcp-src/server/deploy/infomaniak
#   sudo EXPECT_V6=2001:1600:18:207::190 ./check-ipv6.sh
#
# Everything here is read-only. It changes nothing.

set -uo pipefail

# ---------------------------------------------------------------------------
# WHERE THIS RUNS: on the VPS, over SSH, as root.
# Not on your Mac -- it inspects this host's own network stack and firewall.
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
    printf '\033[1;31mxx\033[0m  This script runs ON THE VPS, not on macOS.\n\n' >&2
    printf '    ssh root@179.237.107.22\n' >&2
    printf '    cd /opt/ats-mcp-src/server/deploy/infomaniak && sudo %s\n\n' \
        "$(basename "$0")" >&2
    exit 2
fi


EXPECT_V6="${EXPECT_V6:-}"
HOSTS_TO_COMPARE="${HOSTS_TO_COMPARE:-vault.lopes.me couch.lopes.me status.lopes.me}"

ok()   { printf '\033[1;32m ok \033[0m %s\n' "$*"; }
bad()  { printf '\033[1;31mFAIL\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); }
warn() { printf '\033[1;33mwarn\033[0m %s\n' "$*"; }
info() { printf '     %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

FAILED=0

head_ "1. Is a global IPv6 address assigned to this host?"
V6_ADDRS="$(ip -6 addr show scope global 2>/dev/null \
            | awk '/inet6/{print $2}' | cut -d/ -f1 | grep -v '^fe80' || true)"
if [ -z "$V6_ADDRS" ]; then
    bad "No global IPv6 address on any interface."
    info "The VPS has no usable IPv6. Enable it in the Infomaniak Manager"
    info "(VPS → Network) before going further. Nothing else here will pass."
else
    while read -r a; do ok "address $a"; done <<<"$V6_ADDRS"
    if [ -n "$EXPECT_V6" ]; then
        if grep -qxF "$EXPECT_V6" <<<"$V6_ADDRS"; then
            ok "matches the address you intend to publish ($EXPECT_V6)"
        else
            bad "you intend to publish $EXPECT_V6, which is NOT on this host."
            info "Publishing an AAAA that does not point here guarantees failure."
        fi
    fi
fi

head_ "2. Is there a default IPv6 route?"
if ip -6 route show default 2>/dev/null | grep -q .; then
    ok "$(ip -6 route show default | head -n1)"
else
    bad "No default IPv6 route — outbound v6 cannot work, and neither can ACME."
fi

head_ "3. Does outbound IPv6 actually work?"
if command -v curl >/dev/null 2>&1; then
    _seen="$(curl -6 -fsS --max-time 10 https://ipv6.icanhazip.com 2>/dev/null || echo '')"
    if [ -n "$_seen" ]; then
        ok "outbound v6 works; the internet sees $_seen"
    else
        warn "Could not reach ipv6.icanhazip.com over IPv6."
        info "Outbound is not what Let's Encrypt needs (it connects INBOUND),"
        info "but a total lack of v6 connectivity is a strong warning sign."
    fi
else
    warn "curl not installed; skipping."
fi

head_ "4. Is ufw allowing IPv6 at all?"
if command -v ufw >/dev/null 2>&1; then
    if grep -qi '^IPV6=yes' /etc/default/ufw 2>/dev/null; then
        ok "IPV6=yes in /etc/default/ufw (rules apply to v6 as well as v4)"
    else
        bad "IPV6 is not enabled in /etc/default/ufw."
        info "Fix:  sudo sed -i 's/^IPV6=.*/IPV6=yes/' /etc/default/ufw"
        info "      sudo ufw disable && sudo ufw --force enable"
    fi
    if ufw status 2>/dev/null | grep -qE '(^|\s)(80|443)/tcp.*\(v6\)'; then
        ok "ufw has explicit (v6) rules for 80/443"
    elif ufw status 2>/dev/null | grep -qE '(^|\s)(80|443)/tcp'; then
        warn "ufw lists 80/443 but no (v6) entries — check 'sudo ufw status verbose'."
    else
        bad "ufw does not appear to allow 80/443 at all."
    fi
else
    warn "ufw not installed; skipping."
fi

head_ "5. Is Caddy actually listening on IPv6?"
if command -v ss >/dev/null 2>&1; then
    _l="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E '^\[?(::|\[::\])' || true)"
    if grep -qE '\[::\]:(80|443)|::.*:(80|443)' <<<"$_l"; then
        ok "listening on v6 for 80/443"
        ss -ltnH 2>/dev/null | awk '$4 ~ /::/ && ($4 ~ /:80$/ || $4 ~ /:443$/) {print "     " $4}'
    else
        # A dual-stack wildcard socket shows as *:80 on some kernels.
        if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE '^\*:(80|443)$'; then
            ok "wildcard socket on 80/443 (dual-stack; serves v6 too)"
        else
            bad "Nothing is listening on IPv6 port 80/443."
            info "Caddy binds all interfaces by default, so this usually means"
            info "Caddy is down. Check: systemctl status caddy"
        fi
    fi
else
    warn "ss not installed; skipping."
fi

head_ "6. Do the sibling hosts publish AAAA?"
for h in $HOSTS_TO_COMPARE; do
    _a6="$(getent ahostsv6 "$h" 2>/dev/null | awk 'NR==1{print $1}' || true)"
    if [ -n "$_a6" ]; then ok "$h → $_a6"; else info "$h has no AAAA (A-only)"; fi
done

# ---------------------------------------------------------------------------
_addr="${EXPECT_V6:-$(head -n1 <<<"$V6_ADDRS")}"
# cat prints \033 literally, so the escapes have to be real characters by the
# time the heredoc is expanded.
_HL="$(printf '\033[1;33m')"; _RS="$(printf '\033[0m')"
printf '\n\033[1m%s\033[0m\n' "7. The one test this script CANNOT do for you"
cat <<EOF

  Everything above is local. None of it proves the Infomaniak panel firewall
  permits inbound IPv6 — that is a separate layer from ufw, and it is the most
  likely thing to be blocking you.

  ${_HL}>>> RUN THE NEXT COMMAND ON YOUR MAC, NOT ON THIS SERVER. <<<${_RS}

  Run it here and the packet never leaves the box: it bypasses both firewalls
  and answers even when inbound IPv6 is completely blocked. A false pass is
  worse than no test.

  On your Mac (or a phone on mobile data — anything with IPv6):

      curl -6 -sS -o /dev/null -w '%{http_code}\\n' \\
          "http://[${_addr:-YOUR_V6_ADDR}]/"

  ANY response — 200, 308, 404 — means inbound IPv6 on port 80 reaches Caddy,
  which is what ACME needs. A hang or "Couldn't connect" means it is blocked.

  Repeat for 443:

      curl -6 -k -sS -o /dev/null -w '%{http_code}\\n' \\
          "https://[${_addr:-YOUR_V6_ADDR}]/"

  (-k because you are connecting by address, so the certificate will not match
  the name. That is expected and fine for this test.)

  No IPv6 at hand? Use an external tester such as https://ipv6-test.com/validate.php
  or https://www.ipv6scanner.com/ against ${_addr:-your address}.

EOF

if [ "$FAILED" -gt 0 ]; then
    printf '\033[1;31m%s check(s) failed — do NOT add the AAAA record yet.\033[0m\n\n' "$FAILED"
    exit 1
fi
printf '\033[1;32mLocal checks passed.\033[0m Do the external test above, and if it\n'
printf 'answers, add the AAAA record and run deploy-ats-mcp.sh.\n\n'
