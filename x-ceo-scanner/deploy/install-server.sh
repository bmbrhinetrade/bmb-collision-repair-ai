#!/usr/bin/env bash
# ceoscan one-command installer for a Linux server (Amazon EC2 / Lightsail, or any Ubuntu/Debian box).
# ALERTS ONLY: ceoscan never places trades and never connects to a brokerage.
#
# On the server (e.g. the browser terminal from "EC2 Instance Connect" or Lightsail "Connect using SSH"):
#
#   curl -fsSL https://raw.githubusercontent.com/bmbrhinetrade/bmb-collision-repair-ai/claude/x-ceo-post-scanner-61tt30/x-ceo-scanner/deploy/install-server.sh -o install-ceoscan.sh
#   bash install-ceoscan.sh --email you@example.com
#
# What it does (safe to run again - it updates the code and keeps your .env):
#   1. installs git + Docker + the Docker Compose plugin (Amazon Linux 2023/2, Ubuntu, Debian)
#   2. adds 1 GB of swap on small servers (< 1.8 GB RAM) so the image build does not run out of memory
#   3. downloads ceoscan into ~/ceoscan (or --dir)
#   4. creates x-ceo-scanner/.env: asks for your X bearer token, Anthropic API key and Gmail App Password
#      (typed input is hidden and never printed); keeps an existing .env
#   5. builds the image, sends a test phone alert and a test Claude Duet email, starts ceoscan
#      (restarts automatically after crashes and reboots) and shows its status
#
# Options:
#   --email ADDRESS     your Gmail / Google Workspace address (sender and the inbox Claude Duet reads)
#   --ntfy-topic NAME   ntfy topic for phone pushes (default: a new random private topic)
#   --dir PATH          install directory (default: ~/ceoscan)
#   --branch NAME       git branch (default: claude/x-ceo-post-scanner-61tt30)
#   --yes               no questions: secrets come from the environment variables X_BEARER_TOKEN,
#                       ANTHROPIC_API_KEY and SMTP_PASSWORD; skips the "press Enter" pauses
#   --only-install-docker   install git + Docker + Compose, then stop
#   --skip-docker-install, --skip-tests, --no-start   (for testing the script)
set -euo pipefail

REPO_URL="https://github.com/bmbrhinetrade/bmb-collision-repair-ai.git"
BRANCH="claude/x-ceo-post-scanner-61tt30"
INSTALL_DIR="${HOME}/ceoscan"
EMAIL=""
NTFY_TOPIC_ARG=""
ASSUME_YES=0
SKIP_DOCKER_INSTALL=0
SKIP_TESTS=0
NO_START=0
ONLY_DOCKER=0
# Pinned Compose / Buildx plugin versions, used only where the OS has no package for them.
COMPOSE_VERSION="v2.29.7"
BUILDX_VERSION="v0.17.1"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    WARNING: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --email) EMAIL="${2:-}"; shift 2 ;;
        --ntfy-topic) NTFY_TOPIC_ARG="${2:-}"; shift 2 ;;
        --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
        --branch) BRANCH="${2:-}"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --no-start) NO_START=1; shift ;;
        --only-install-docker) ONLY_DOCKER=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux servers"
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
    command -v sudo >/dev/null 2>&1 || die "run as a user with sudo rights (e.g. ec2-user or ubuntu)"
    SUDO="sudo"
fi

# Interactive input always comes from the terminal, even if this script is piped into bash.
TTY="/dev/tty"
if [ "$ASSUME_YES" -eq 0 ] && [ "$ONLY_DOCKER" -eq 0 ] && ! { [ -r "$TTY" ] && : < "$TTY"; } 2>/dev/null; then
    die "no terminal for questions; run it as 'bash install-ceoscan.sh' in an interactive shell, or use --yes"
fi
ask() {  # ask VAR "question" [default]
    local __var="$1" __q="$2" __def="${3:-}" __ans=""
    if [ "$ASSUME_YES" -eq 1 ]; then printf -v "$__var" '%s' "$__def"; return; fi
    if [ -n "$__def" ]; then printf '%s [%s]: ' "$__q" "$__def" > "$TTY"; else printf '%s: ' "$__q" > "$TTY"; fi
    IFS= read -r __ans < "$TTY" || true
    printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_secret() {  # ask_secret VAR "question" ENVNAME - hidden input; never echoed or logged
    local __var="$1" __q="$2" __env="$3" __ans=""
    if [ "$ASSUME_YES" -eq 1 ]; then printf -v "$__var" '%s' "${!__env:-}"; return; fi
    printf '%s (typing is hidden; paste and press Enter): ' "$__q" > "$TTY"
    IFS= read -rs __ans < "$TTY" || true
    printf '\n' > "$TTY"
    printf -v "$__var" '%s' "$__ans"
}
pause() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    printf '%s ' "${1:-Press Enter to continue}" > "$TTY"
    local _x; IFS= read -r _x < "$TTY" || true
}

# --------------------------------------------------------------------------- 1. packages + Docker
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
OS_ID="${ID:-unknown}"; OS_VER="${VERSION_ID:-}"
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) DL_ARCH="x86_64"; BX_ARCH="amd64" ;;
    aarch64|arm64) DL_ARCH="aarch64"; BX_ARCH="arm64" ;;
    *) die "unsupported CPU architecture: $ARCH" ;;
esac

install_cli_plugin() {  # install_cli_plugin NAME URL
    local name="$1" url="$2" dir="/usr/local/lib/docker/cli-plugins"
    info "installing the Docker $name plugin from GitHub ($url)"
    $SUDO mkdir -p "$dir"
    $SUDO curl -fsSL --retry 3 -o "$dir/docker-$name" "$url" || die "could not download the Docker $name plugin"
    $SUDO chmod 755 "$dir/docker-$name"
}

ensure_compose() {
    if ! $SUDO docker compose version >/dev/null 2>&1; then
        install_cli_plugin compose \
            "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${DL_ARCH}"
    fi
    if ! $SUDO docker buildx version >/dev/null 2>&1; then
        install_cli_plugin buildx \
            "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${BX_ARCH}"
    fi
}

if [ "$SKIP_DOCKER_INSTALL" -eq 0 ]; then
    say "1/5 Installing git and Docker (${PRETTY_NAME:-$OS_ID $OS_VER}, $ARCH)"
    case "$OS_ID" in
        amzn)
            if command -v dnf >/dev/null 2>&1; then
                $SUDO dnf install -y -q git docker curl-minimal >/dev/null 2>&1 || $SUDO dnf install -y -q git docker
            else
                $SUDO yum install -y -q git docker
            fi
            ;;
        ubuntu|debian)
            export DEBIAN_FRONTEND=noninteractive
            $SUDO apt-get update -qq
            $SUDO apt-get install -y -qq git curl ca-certificates >/dev/null
            if ! command -v docker >/dev/null 2>&1; then
                $SUDO apt-get install -y -qq docker.io >/dev/null
            fi
            $SUDO apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 \
                || $SUDO apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
            $SUDO apt-get install -y -qq docker-buildx >/dev/null 2>&1 \
                || $SUDO apt-get install -y -qq docker-buildx-plugin >/dev/null 2>&1 || true
            ;;
        *)
            command -v docker >/dev/null 2>&1 \
                || die "unsupported Linux ($OS_ID): install Docker yourself, then run this again with --skip-docker-install"
            ;;
    esac
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || $SUDO systemctl start docker
    fi
    ensure_compose
    if [ -n "$SUDO" ] && ! id -nG "$(id -un)" | tr ' ' '\n' | grep -qx docker; then
        $SUDO usermod -aG docker "$(id -un)" 2>/dev/null || true   # lets you run docker without sudo after re-login
    fi
else
    say "1/5 Skipping the Docker installation (--skip-docker-install)"
fi
if [ "$ONLY_DOCKER" -eq 1 ]; then
    command -v git >/dev/null 2>&1 && command -v docker >/dev/null 2>&1 || die "git or docker did not install"
    info "installed: $(git --version); $(docker --version 2>/dev/null)"
    info "compose plugin: $(ls /usr/local/lib/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-compose /usr/lib/docker/cli-plugins/docker-compose 2>/dev/null | head -1)"
    info "buildx plugin:  $(ls /usr/local/lib/docker/cli-plugins/docker-buildx /usr/libexec/docker/cli-plugins/docker-buildx /usr/lib/docker/cli-plugins/docker-buildx 2>/dev/null | head -1)"
    exit 0
fi
command -v git >/dev/null 2>&1 || die "git is missing"
$SUDO docker info >/dev/null 2>&1 || die "Docker is installed but not running (try: sudo systemctl start docker)"
$SUDO docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing"
info "$($SUDO docker --version), $($SUDO docker compose version | head -1)"

# --------------------------------------------------------------------------- 2. swap on small servers
mem_mb=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo)
if [ "${mem_mb:-0}" -lt 1800 ] && [ "${swap_mb:-0}" -lt 512 ] && [ ! -e /swapfile ]; then
    say "2/5 Adding 1 GB of swap (this server has ${mem_mb} MB of RAM)"
    make_swap() {  # make_swap fallocate|dd
        if [ "$1" = fallocate ]; then $SUDO fallocate -l 1G /swapfile 2>/dev/null || return 1
        else $SUDO dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none 2>/dev/null || return 1; fi
        $SUDO chmod 600 /swapfile && $SUDO mkswap /swapfile >/dev/null 2>&1 && $SUDO swapon /swapfile 2>/dev/null
    }
    # fallocate is instant but some filesystems (older XFS) refuse such a file for swap; dd always works
    if make_swap fallocate || { $SUDO swapoff /swapfile 2>/dev/null; $SUDO rm -f /swapfile; make_swap dd; }; then
        grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
        info "swap is on"
    else
        warn "could not turn swap on (not allowed here?); continuing without it"
        $SUDO rm -f /swapfile
    fi
else
    say "2/5 Memory: ${mem_mb} MB RAM, ${swap_mb} MB swap - no swap needed"
fi

# --------------------------------------------------------------------------- 3. code
say "3/5 Getting ceoscan (branch $BRANCH) into $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
    changed="$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)"
    if [ -n "$changed" ]; then
        # e.g. config/watchlist.yaml edited here or updated by `verify --write` - never overwrite it
        warn "files were changed on this server, so the code was NOT updated (your changes are kept):"
        printf '%s\n' "$changed" | sed 's/^/        /' >&2
        warn "to update anyway: cd $INSTALL_DIR && git stash && bash x-ceo-scanner/deploy/install-server.sh, then git stash pop"
    else
        git -C "$INSTALL_DIR" fetch -q --depth 1 origin "$BRANCH"
        git -C "$INSTALL_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
        info "updated to $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
    fi
else
    git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    info "downloaded $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
fi
APP="$INSTALL_DIR/x-ceo-scanner"
[ -f "$APP/docker-compose.yml" ] || die "$APP/docker-compose.yml not found - wrong branch?"
cd "$APP"

# The container user: your own UID/GID (never root; root installs use 10001).
if [ "$(id -u)" -eq 0 ]; then RUN_UID=10001; RUN_GID=10001; else RUN_UID="$(id -u)"; RUN_GID="$(id -g)"; fi

# --------------------------------------------------------------------------- 4. .env
set_env() {  # set_env KEY VALUE - replace or append KEY=VALUE in .env (value written verbatim)
    local key="$1" val="$2" tmp
    tmp="$(mktemp "$APP/.env.XXXXXX")"
    chmod 600 "$tmp"
    if [ -f .env ]; then grep -v "^${key}=" .env > "$tmp" || true; fi
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv -f "$tmp" .env
}
clean_secret() { printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

if [ -f .env ]; then
    say "4/5 Keeping your existing .env (delete it and run this again to start over)"
    grep -q '^CEOSCAN_UID=' .env || set_env CEOSCAN_UID "$RUN_UID"
    grep -q '^CEOSCAN_GID=' .env || set_env CEOSCAN_GID "$RUN_GID"
else
    say "4/5 Creating .env (your private keys - stored only on this server, readable only by you)"
    umask 077
    cp .env.example .env
    chmod 600 .env

    while [ -z "$EMAIL" ] || ! printf '%s' "$EMAIL" | grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; do
        [ "$ASSUME_YES" -eq 1 ] && die "--email is required with --yes"
        ask EMAIL "Your Gmail / Google Workspace address (sends the Duet emails and receives them)"
    done
    if [ -n "$NTFY_TOPIC_ARG" ]; then NTFY_TOPIC="$NTFY_TOPIC_ARG"; else
        # No `tr < /dev/urandom | head`: under pipefail the SIGPIPE to tr would abort the script.
        rand=""
        while [ "${#rand}" -lt 24 ]; do rand="${rand}$(LC_ALL=C tr -dc 'a-z0-9' < <(head -c 1024 /dev/urandom))"; done
        NTFY_TOPIC="ceoscan-${rand:0:24}"
    fi
    printf '%s' "$NTFY_TOPIC" | grep -Eq '^[A-Za-z0-9_-]{1,64}$' || die "invalid ntfy topic: use letters, digits, - and _"

    echo
    info "Three secrets are needed. Each is typed hidden and saved only in $APP/.env."
    info "  1. X bearer token:   console.x.com > your app > Keys and tokens > Bearer Token"
    info "  2. Anthropic key:    console.anthropic.com > API Keys > Create Key (starts with sk-ant-)"
    info "  3. Gmail App Password for ${EMAIL}: myaccount.google.com > Security > 2-Step Verification"
    info "     (turn on) > App passwords > create one named ceoscan (16 letters; spaces are fine)"
    echo
    X_TOKEN=""; ANTH_KEY=""; APP_PW=""
    ask_secret X_TOKEN "1. X bearer token" X_BEARER_TOKEN
    ask_secret ANTH_KEY "2. Anthropic API key (Enter to skip: rules-only scoring)" ANTHROPIC_API_KEY
    ask_secret APP_PW "3. Gmail App Password (Enter to skip: no Claude Duet emails)" SMTP_PASSWORD
    X_TOKEN="$(clean_secret "$X_TOKEN")"; ANTH_KEY="$(clean_secret "$ANTH_KEY")"
    APP_PW="$(clean_secret "$APP_PW" | tr -d ' ')"
    [ -n "$X_TOKEN" ] || die "the X bearer token is required"
    case "$ANTH_KEY" in ""|sk-ant-*) ;; *) warn "the Anthropic key does not start with sk-ant- - check it later with the test commands" ;; esac
    if [ -n "$APP_PW" ] && [ "${#APP_PW}" -ne 16 ]; then warn "a Gmail App Password has 16 letters; yours has ${#APP_PW}"; fi

    set_env X_BEARER_TOKEN "$X_TOKEN"
    set_env ANTHROPIC_API_KEY "$ANTH_KEY"
    set_env NTFY_TOPIC "$NTFY_TOPIC"
    set_env SMTP_HOST "smtp.gmail.com"
    set_env SMTP_PORT "587"
    set_env SMTP_USERNAME "$EMAIL"
    set_env SMTP_PASSWORD "$APP_PW"
    set_env ALERT_EMAIL_FROM "$EMAIL"
    set_env ALERT_EMAIL_TO "$EMAIL"
    set_env DUET_FEED_TO "$EMAIL"
    set_env CEOSCAN_UID "$RUN_UID"
    set_env CEOSCAN_GID "$RUN_GID"
    unset X_TOKEN ANTH_KEY APP_PW
    info "saved .env (permissions 600)"
fi
mkdir -p data
if [ "$(stat -c %u data)" != "$RUN_UID" ]; then $SUDO chown -R "$RUN_UID:$RUN_GID" data config; fi
TOPIC_NOW="$(grep '^NTFY_TOPIC=' .env | head -1 | cut -d= -f2-)"

# --------------------------------------------------------------------------- 5. build, test, start
say "5/5 Building the ceoscan image (2-5 minutes the first time)"
$SUDO docker compose build -q
info "image built"

TEST_ALERT="skipped"; TEST_DUET="skipped"; ACCESS="skipped"
if [ "$SKIP_TESTS" -eq 0 ]; then
    if [ -n "$TOPIC_NOW" ]; then
        echo
        info "PHONE PUSH: install the free \"ntfy\" app on your phone, tap +, and subscribe to this topic:"
        info ""
        info "      $TOPIC_NOW"
        info ""
        info "(keep it private: anyone who knows the topic name can read your alerts)"
        pause "Press Enter when you have subscribed, to send a test alert..."
    fi
    info "checking X API access..."
    if $SUDO docker compose run --rm -T ceoscan check-access; then ACCESS="ok"; else ACCESS="FAILED (see above)"; fi
    info "sending a test phone alert..."
    if $SUDO docker compose run --rm -T ceoscan test-alert --message "ceoscan is installed on your server"; then
        TEST_ALERT="sent"; else TEST_ALERT="FAILED (see above)"; fi
    if grep -q '^SMTP_PASSWORD=..*' .env; then
        info "sending the Claude Duet test email..."
        if $SUDO docker compose run --rm -T ceoscan test-duet; then TEST_DUET="sent"; else TEST_DUET="FAILED (see above)"; fi
    else
        TEST_DUET="skipped (no Gmail App Password)"
    fi
fi

if [ "$NO_START" -eq 0 ]; then
    info "starting ceoscan..."
    $SUDO docker compose up -d
    health=""
    for _ in $(seq 1 30); do
        health="$(curl -s -m 3 http://127.0.0.1:8080/health 2>/dev/null || true)"
        [ -n "$health" ] && break
        sleep 2
    done
    STATUS="$(printf '%s' "$health" | sed -n 's/.*"status": *"\([a-z_]*\)".*/\1/p' | head -1)"
else
    STATUS="not started (--no-start)"
fi

say "Done"
cat <<EOF
    ceoscan folder:   $APP
    X API access:     $ACCESS
    test phone push:  $TEST_ALERT
    test Duet email:  $TEST_DUET
    ceoscan status:   ${STATUS:-unknown} (stream connects within a minute; "degraded" right after start is normal)

    Useful commands (run them in $APP):
      sudo docker compose logs -f --tail 50      # live log (Ctrl-C to leave)
      curl -s 127.0.0.1:8080/health              # status
      sudo docker compose restart ceoscan        # restart after editing .env or config/
      sudo docker compose down                   # stop
      bash deploy/install-server.sh --dir $INSTALL_DIR   # update to the newest version later

    ceoscan restarts by itself after a crash or a server reboot. It only makes outgoing
    connections, so no AWS firewall (security group) port has to be opened.
EOF
