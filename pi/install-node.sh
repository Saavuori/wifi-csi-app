#!/usr/bin/env bash
#
# Turn this Raspberry Pi into a CSI node: patch the Wi-Fi firmware with nexmon_csi, install
# the forwarder, and run both from systemd.
#
# Normally invoked by the top-level install.sh via --node, but it stands alone:
#
#     sudo pi/install-node.sh --server 192.168.1.10
#
# The firmware build is long (20-40 minutes on a Pi) and, on a Pi 5, needs one reboot before
# it can start. The script is resumable: it records which stage it reached and picks up there,
# so running it again after the reboot continues rather than starting over.
#
#     --server HOST      where to send frames (default: this host)
#     --port N           server UDP port (default 5566)
#     --node-id N        node id, 1..254 (default 20)
#     --iface NAME       wireless interface (default wlan0)
#     --ap MAC           only measure frames from this transmitter (default: the associated AP)
#     --probe-hz N       ping rate that generates the traffic to measure (default 100)
#     --stimulus MODE    Ethernet multicast fallback: auto, always or off (default auto)
#     --stimulus-iface NAME  wired interface it emits on (default eth0)
#     --stimulus-hz N    its rate while armed (default 50)
#     --repo URL         nexmon_csi fork to build (default: Saavuori/nexmon_csi)
#     --branch NAME      branch of that fork
#     --nexmon-repo URL  base nexmon tree to build it against (default: seemoo-lab/nexmon)
#     --skip-build       assume the firmware is already patched; just install the service
#     --uninstall        remove the service and restore the stock firmware
#     --yes              assume yes for prompts
#
set -euo pipefail

# Two repositories, and only one of them is ours. The base nexmon tree — the toolchain and the
# firmware patching framework — is used unmodified from upstream. The CSI patch that goes into
# patches/$CHIP/$FW_VERSION is our fork, because upstream's takes the Wi-Fi connection down; see
# pi/README.md. Both are overridable, by flag or by environment, so a different fork or a local
# mirror needs no edit to this file.
NEXMON_REPO="${CSI_NEXMON_BASE_REPO:-https://github.com/seemoo-lab/nexmon.git}"
CSI_REPO="${CSI_NEXMON_REPO:-https://github.com/Saavuori/nexmon_csi.git}"
# master, not the original feature branch. That branch is 0 commits ahead of master and 3
# behind, so it is a strict subset — and one of the three it is missing is the fix that makes
# nexutil able to reach the firmware at all on a current kernel. Pinning it is what made an
# otherwise complete install produce no CSI.
CSI_BRANCH="${CSI_NEXMON_BRANCH:-master}"

# nexmon patches one firmware version per chip directory, and the CSI patch decides which.
# This must be 7_45_189: the fork's src/version.c declares `char version[]` only inside
#     #if NEXMON_CHIP == CHIP_VER_BCM43455c0 && NEXMON_FW_VERSION == FW_VER_7_45_189
# and every one of its firmware-version references is that same pairing. Against 7_45_206 —
# which nexmon does ship a firmware directory for, so it looks plausible — no #if branch
# matches, while the GenericPatch4(version_patch, version) below the #endif still refers to
# the symbol. The build dies 20 minutes in with "error: 'version' undeclared here".
CHIP="bcm43455c0"
FW_VERSION="7_45_189"

PREFIX="/opt/csi-node"
NEXMON_DIR="$PREFIX/nexmon"
STATE_DIR="/var/lib/csi-node"
STATE_FILE="$STATE_DIR/stage"
ENV_FILE="/etc/default/csi-node"
SERVICE="/etc/systemd/system/csi-node.service"

SERVER=""
PORT="${CSI_UDP_PORT:-5566}"
# The server's HTTP API, which the node polls for channel and stimulus control. Defaults to the
# server host on this port; the control loop is disabled only if this ends up empty.
HTTP_PORT="${CSI_HTTP_PORT:-8080}"
CONTROL_URL="${CSI_CONTROL_URL:-}"
NODE_ID="${CSI_NODE_ID:-20}"
IFACE="${CSI_IFACE:-wlan0}"
AP_MAC=""
PROBE_HZ="${CSI_PROBE_HZ:-100}"
STIMULUS="${CSI_STIMULUS:-auto}"
STIMULUS_IFACE="${CSI_STIMULUS_IFACE:-eth0}"
STIMULUS_HZ="${CSI_STIMULUS_HZ:-50}"
SKIP_BUILD=0
UNINSTALL=0
ASSUME_YES=0

bold=""; dim=""; red=""; green=""; yellow=""; reset=""
if [ -t 1 ]; then
    bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'
    yellow=$'\033[33m'; reset=$'\033[0m'
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$bold" "$reset" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$reset" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$reset" "$*"; }
die()  { printf '%serror:%s %s\n' "$red" "$reset" "$*" >&2; exit 1; }

ask() {
    local prompt="$1" default="${2:-n}" hint="[y/N]" reply
    if [ "$ASSUME_YES" = 1 ]; then return 0; fi
    [ "$default" = "y" ] && hint="[Y/n]"
    if [ ! -r /dev/tty ]; then
        say "  $prompt $hint (no terminal, assuming $default)"
        [ "$default" = "y" ]
        return
    fi
    printf '  %s %s ' "$prompt" "$hint" > /dev/tty
    read -r reply < /dev/tty || reply=""
    case "${reply:-$default}" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server)     SERVER="${2:?--server needs a host}"; shift ;;
        --port)       PORT="${2:?--port needs a number}"; shift ;;
        --http-port)  HTTP_PORT="${2:?--http-port needs a number}"; shift ;;
        --control-url) CONTROL_URL="${2:?--control-url needs a URL}"; shift ;;
        --node-id)    NODE_ID="${2:?--node-id needs a number}"; shift ;;
        --iface)      IFACE="${2:?--iface needs a name}"; shift ;;
        --ap)         AP_MAC="${2:?--ap needs a MAC}"; shift ;;
        --probe-hz)   PROBE_HZ="${2:?--probe-hz needs a number}"; shift ;;
        --stimulus)   STIMULUS="${2:?--stimulus needs auto, always or off}"; shift ;;
        --stimulus-iface) STIMULUS_IFACE="${2:?--stimulus-iface needs a name}"; shift ;;
        --stimulus-hz)    STIMULUS_HZ="${2:?--stimulus-hz needs a number}"; shift ;;
        --repo)       CSI_REPO="${2:?--repo needs a URL}"; shift ;;
        --branch)     CSI_BRANCH="${2:?--branch needs a name}"; shift ;;
        --nexmon-repo) NEXMON_REPO="${2:?--nexmon-repo needs a URL}"; shift ;;
        --skip-build) SKIP_BUILD=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        --yes|-y)     ASSUME_YES=1 ;;
        -h|--help)    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

[ "$(id -u)" = 0 ] || die "this needs root: sudo $0 $*"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------------------
# Stages. The firmware build survives a reboot; the reboot is what the Pi 5 needs before it
# can start. Recording the stage is what makes "run it again" the recovery procedure.
# ---------------------------------------------------------------------------------------

stage_done() {
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$1" > "$STATE_FILE"
}

stage_reached() {
    [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$1" ]
}

# ---------------------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------------------

pi_model() {
    tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown"
}

check_hardware() {
    step "Checking the radio"
    local model
    model="$(pi_model)"
    say "  $model"

    case "$model" in
        *"Raspberry Pi 5"*|*"Raspberry Pi 4"*|*"Raspberry Pi 3 Model B Plus"*|*"Compute Module 4"*)
            ok "this board carries the BCM43455, which nexmon_csi patches"
            ;;
        *"Raspberry Pi"*)
            die "this Pi's Wi-Fi chip is not one nexmon_csi supports. CSI capture needs the
       BCM43455, which means a Pi 3B+, 4, 5 or CM4. A Pi Zero, Zero 2 W or an
       original 3B will not work no matter what is installed."
            ;;
        *)
            warn "not a Raspberry Pi as far as /proc/device-tree/model says; continuing anyway"
            ;;
    esac

    [ -d "/sys/class/net/$IFACE" ] || die "no interface $IFACE (pass --iface)"
}

# Raspberry Pi OS on a Pi 5 boots a 16 KB page kernel by default. nexmon's firmware patching
# toolchain needs 4 KB pages; on the 16 KB kernel the build produces a firmware that does not
# load, which presents as Wi-Fi simply being gone after a reboot rather than as a build error.
check_pagesize() {
    local pagesize
    pagesize="$(getconf PAGESIZE 2>/dev/null || echo 4096)"
    if [ "$pagesize" = "4096" ]; then
        ok "4 KB pages"
        return 0
    fi

    step "Switching to the 4 KB page kernel"
    say "  This Pi boots a ${pagesize}-byte page kernel. nexmon needs 4096."
    local config="/boot/firmware/config.txt"
    [ -f "$config" ] || config="/boot/config.txt"
    [ -f "$config" ] || die "cannot find config.txt to edit"

    if grep -q '^kernel=kernel8.img' "$config"; then
        warn "$config already selects kernel8.img but the running kernel still has
    ${pagesize}-byte pages — you are running the old one. Reboot and run this again."
        exit 0
    fi

    if ! ask "Add 'kernel=kernel8.img' to $config and reboot?" y; then
        die "cannot continue on a ${pagesize}-byte page kernel"
    fi

    cp "$config" "$config.csi-backup"
    printf '\n# Added by csi-node: nexmon needs a 4 KB page kernel.\nkernel=kernel8.img\n' \
        >> "$config"
    stage_done "pagesize"
    ok "set; the old config is at $config.csi-backup"
    say ""
    say "${bold}Reboot, then run this again to continue.${reset}"
    say "  sudo reboot"
    exit 0
}

# ---------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------

install_deps() {
    step "Installing build dependencies"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # git/gcc/make build the toolchain; the rest are nexmon's own requirements. flex, bison
    # and libfl-dev build its C parser; texinfo and libtool build the cross-binutils. xxd is
    # its own package since Bullseye — it used to arrive with vim-common, so a clean Pi does
    # not have it, and nexmon's firmware blob extraction fails with `xxd: command not found`
    # partway into a build that has already run for several minutes.
    # libnl-3-dev and libnl-genl-3-dev are for nexutil's vendor-command build; see
    # install_nexutil, which cannot talk to the driver without it. libcap2-bin provides
    # setcap. pkg-config is how nexutil's Makefile finds the netlink libraries.
    apt-get install -y -qq \
        git gcc make automake libtool texinfo flex bison libfl-dev \
        gawk qpdf tcpdump iw xxd python3 python3-numpy \
        pkg-config libnl-3-dev libnl-genl-3-dev libcap2-bin \
        raspberrypi-kernel-headers > /dev/null 2>&1 || \
    apt-get install -y -qq \
        git gcc make automake libtool texinfo flex bison libfl-dev \
        gawk qpdf tcpdump iw xxd python3 python3-numpy \
        pkg-config libnl-3-dev libnl-genl-3-dev libcap2-bin > /dev/null
    ok "installed"

    # An old release, where xxd still came from vim-common. Cheaper to say so now than to have
    # the build stop on it ten minutes in.
    command -v xxd > /dev/null 2>&1 || \
        warn "xxd is still not on PATH and nexmon's blob extraction needs it.
    Try: apt-get install vim-common"

    install_armhf_runtime
}

# nexmon's cross-compiler is a 32-bit ARM binary — gcc-arm-none-eabi-5_4-2016q2-linux-armv7l —
# and setup_env.sh selects it on aarch64 too. 64-bit Raspberry Pi OS has none of what it needs
# to run, and the first symptom is deeply misleading:
#
#     .../bin/arm-none-eabi-gcc: not found
#
# The file is right there. "not found" is the shell reporting a missing ELF *interpreter*,
# /lib/ld-linux-armhf.so.3, which arrives with libc6:armhf.
install_armhf_runtime() {
    [ "$(uname -m)" = "aarch64" ] || return 0

    step "Installing the 32-bit runtime for nexmon's toolchain"
    dpkg --print-foreign-architectures 2>/dev/null | grep -qx armhf \
        || dpkg --add-architecture armhf
    apt-get update -qq
    apt-get install -y -qq libc6:armhf libmpc3:armhf libgmp10:armhf \
        libgmp-dev:armhf crossbuild-essential-armhf > /dev/null 2>&1 \
        || warn "some armhf packages did not install; the toolchain may not run"
    ok "armhf runtime installed"
}

# cc1 also links libisl.so.10 and libmpfr.so.4. Those are long gone from Debian — trixie ships
# libisl23 and libmpfr6 — so they are built from the sources nexmon bundles for exactly this.
# Skipped entirely once they exist, because this is several minutes of compiling.
build_host_libs() {
    [ "$(uname -m)" = "aarch64" ] || return 0
    local prefix="$PREFIX/armhf-libs"
    if [ -e "$prefix/lib/libisl.so.10" ] && [ -e "$prefix/lib/libmpfr.so.4" ]; then
        ok "armhf libisl/libmpfr already built"
        return 0
    fi

    step "Building libisl.so.10 and libmpfr.so.4 for the toolchain"
    say "  ${dim}a few minutes; they are not packaged on any current Debian${reset}"
    mkdir -p "$prefix"

    # --build is explicit because these are 2012-vintage autotools whose config.guess predates
    # aarch64 and bails with "cannot guess build type". The ACLOCAL=:/AUTOCONF=: overrides stop
    # make trying to regenerate with aclocal-1.15, which no current Debian ships.
    local host=arm-linux-gnueabihf build=aarch64-unknown-linux-gnu
    local noregen="ACLOCAL=: AUTOCONF=: AUTOMAKE=: AUTOHEADER=: MAKEINFO=:"

    ( cd "$NEXMON_DIR/buildtools/mpfr-3.1.4" \
        && ./configure --build="$build" --host="$host" --prefix="$prefix" \
             --with-gmp-include=/usr/include/arm-linux-gnueabihf \
             --with-gmp-lib=/usr/lib/arm-linux-gnueabihf \
             --disable-static --enable-shared > /dev/null 2>&1 \
        && make -j"$(nproc)" $noregen > /dev/null 2>&1 \
        && make install $noregen > /dev/null 2>&1 ) \
        || die "could not build libmpfr.so.4 for the toolchain"

    ( cd "$NEXMON_DIR/buildtools/isl-0.10" \
        && ./configure --build="$build" --host="$host" --prefix="$prefix" \
             --with-gmp-prefix=/usr --disable-static --enable-shared > /dev/null 2>&1 \
        && make -j"$(nproc)" $noregen > /dev/null 2>&1 \
        && make install $noregen > /dev/null 2>&1 ) \
        || die "could not build libisl.so.10 for the toolchain"

    printf '%s\n' "$prefix/lib" > /etc/ld.so.conf.d/csi-nexmon-armhf.conf
    ldconfig
    ok "built and registered with ldconfig"
}

# nexmon's b43 disassembly helpers are Python 2. They are invoked by *path* from Makefile.rpi,
# so their shebang is the dependency and grepping the Makefiles for "python" does not reveal it
# — which is how an earlier version of this script concluded python was not needed at all.
#
# Rather than requiring a python2 that Debian no longer ships, convert them. The constructs are
# few and mechanical: print statements, `except X, e:` and the removed `file()` builtin. There
# are no iteritems/has_key/xrange/long/unicode/backticks/octal literals, and every '/' in them
# is in a comment, a regex or an #include path rather than arithmetic, so true-versus-floor
# division cannot silently change a result.
port_b43_tools_to_python3() {
    local dir="$NEXMON_DIR/buildtools/b43-v3/debug"
    local f converted=0
    for f in "$dir/libb43.py" "$dir/b43-beautifier"; do
        [ -f "$f" ] || continue
        # Already converted by a previous run.
        head -1 "$f" | grep -q 'python3' && continue
        cp -n "$f" "$f.py2bak" 2>/dev/null || true
        python3 - "$f" <<'PY' || die "could not port $f to Python 3"
import re, sys, py_compile
path = sys.argv[1]
src = open(path, encoding='utf-8').read()
out = []
for line in src.splitlines():
    if line.lstrip().startswith('#'):
        out.append(line); continue
    new = re.sub(r'^(\s*except\s+[\w.]+)\s*,\s*(\w+)\s*:', r'\1 as \2:', line)
    new = re.sub(r'(?<![\w.])file\(', 'open(', new)
    m = re.match(r'^(\s*)print\s+(?!\()(.+?)\s*$', new)
    if m:
        new = f'{m.group(1)}print({m.group(2)})'
    out.append(new)
text = re.sub(r'^#!.*python\s*$', '#!/usr/bin/env python3',
              '\n'.join(out) + '\n', count=1, flags=re.M)
open(path, 'w', encoding='utf-8').write(text)
# Refuse to leave behind a file that only looks converted.
py_compile.compile(path, doraise=True, cfile=path + '.pyc-check')
PY
        rm -f "$f.pyc-check"
        converted=$((converted + 1))
    done
    [ "$converted" -gt 0 ] && ok "ported $converted b43 build tool(s) to Python 3"
    return 0
}

# Puts the nexmon_csi checkout in CSI_DIR. A global rather than an echoed path, so that the
# progress output goes to the terminal instead of being captured with it.
CSI_DIR=""

fetch_sources() {
    step "Fetching nexmon and the CSI patch"
    mkdir -p "$PREFIX"

    if [ ! -d "$NEXMON_DIR/.git" ]; then
        say "  cloning nexmon"
        git clone --depth 1 "$NEXMON_REPO" "$NEXMON_DIR" > /dev/null 2>&1 \
            || die "could not clone $NEXMON_REPO"
    fi

    local patch_dir="$NEXMON_DIR/patches/$CHIP/$FW_VERSION"
    mkdir -p "$patch_dir"
    CSI_DIR="$patch_dir/nexmon_csi"

    if [ ! -d "$CSI_DIR/.git" ]; then
        say "  cloning $CSI_REPO ($CSI_BRANCH)"
        git clone --depth 1 --branch "$CSI_BRANCH" "$CSI_REPO" "$CSI_DIR" > /dev/null 2>&1 \
            || die "could not clone $CSI_REPO at branch $CSI_BRANCH"
    else
        # A resumed run finds a checkout from the previous one, which may have been made
        # against a different --repo. Re-point origin rather than fetching from whatever it
        # happens to be: building upstream's patch here would come out looking like a build
        # failure much later, in monitor mode with the network gone.
        local origin was_ours=1
        origin="$(git -C "$CSI_DIR" remote get-url origin 2>/dev/null || true)"
        if [ "$origin" != "$CSI_REPO" ]; then
            was_ours=0
            say "  repointing the CSI patch at $CSI_REPO (was ${origin:-unknown})"
            git -C "$CSI_DIR" remote set-url origin "$CSI_REPO" \
                || die "could not point $CSI_DIR at $CSI_REPO"
        fi

        say "  updating the CSI patch ($CSI_BRANCH)"
        # A failed update is survivable when the checkout is already the right tree — an
        # offline resume should build what it has. When it is not, it is fatal: there is no
        # falling back on a checkout of some other fork.
        if git -C "$CSI_DIR" fetch --depth 1 origin "$CSI_BRANCH" > /dev/null 2>&1 \
           && git -C "$CSI_DIR" checkout -q FETCH_HEAD > /dev/null 2>&1; then
            :
        elif [ "$was_ours" = 1 ]; then
            warn "could not update from $CSI_REPO; building the existing checkout"
        else
            die "could not fetch $CSI_BRANCH from $CSI_REPO, and $CSI_DIR is a checkout of
       ${origin:-another repository}. Remove $CSI_DIR and run this again once the
       network is back, rather than building the wrong patch."
        fi
    fi

    ok "sources in $NEXMON_DIR"
    say "  ${dim}base nexmon: $NEXMON_REPO${reset}"
    say "  ${dim}CSI patch:   $CSI_REPO ($CSI_BRANCH)${reset}"
}

build_firmware() {
    local csi_dir="$CSI_DIR"
    step "Building the patched firmware"
    say "  ${dim}20-40 minutes on a Pi. It compiles a cross-toolchain first.${reset}"

    # A plain `make` here builds the cross-toolchain and then descends into *every* chip in
    # firmwares/, so a chip we will never load stops a build that had nothing to do with it.
    # bcm43439a0 is the one that bites: its Makefile extracts a blob with `xxd -r -p > $@`,
    # and because the redirect creates the target before xxd runs, a failure leaves an empty
    # file behind that make then treats as built — so the next run fails differently, in the
    # ucode extractor, with the original cause no longer on screen.
    #
    # buildtools is genuinely shared. The firmware directory is ours alone, so name it.
    ( cd "$NEXMON_DIR" && set +u && . ./setup_env.sh \
        && make -C buildtools \
        && make -C "firmwares/$CHIP/$FW_VERSION" ) \
        || die "the nexmon toolchain build failed. It compiles a cross-toolchain first, so
       this is the long and fragile step. The last few lines above name the missing
       tool or the failing file. Nothing is broken — rerun after fixing and it
       resumes here."

    port_b43_tools_to_python3
    clear_stale_ucode "$csi_dir"

    # Makefile.rpi is the path that does not need a patched brcmfmac, which is what makes
    # this work on current kernels and on the Pi 5 at all.
    ( cd "$csi_dir" && set +u && . "$NEXMON_DIR/setup_env.sh" \
        && make -f Makefile.rpi && make -f Makefile.rpi install-firmware \
        && make -f Makefile.rpi install-csi-tools ) \
        || die "the CSI firmware build failed; see the log above"

    install_nexutil
    ok "firmware patched and installed"
}

# The ucode rule is two commands: b43-dasm writes gen/ucode.asm, then b43-beautifier rewrites
# it through a temporary file. If the beautifier fails, the raw disassembly is already on disk
# and *newer than its prerequisite*, so every later run skips the rule entirely and quietly
# assembles unbeautified code. That surfaces far away, as
#
#     Parser ERROR ... jext COND_RX_IFS2, skip+  /  syntax error
#
# because the beautifier is also what prepends the #include lines defining COND_RX_IFS2.
# Detect the half-built file by its missing preamble and remove it and everything derived.
clear_stale_ucode() {
    local dir="$1" asm="$1/gen/ucode.asm"
    [ -f "$asm" ] || return 0
    head -1 "$asm" | grep -q '#include' && return 0
    warn "gen/ucode.asm was left unbeautified by an earlier failed run; regenerating"
    rm -f "$asm" "$dir/gen/ucode.bin" "$dir/gen/ucode_compressed.bin" \
          "$dir/src/csi.ucode.$CHIP.$FW_VERSION.asm"
}

# install-csi-tools builds makecsiparams and nothing else, but csi-connected.sh needs nexutil
# too and refuses to run without it. nexutil lives in the base nexmon tree, under utilities/,
# which no target in the root Makefile descends into — so nothing builds it and the service
# fails at ExecStartPre with "'nexutil' not found in PATH".
#
# Built with USE_VENDOR_CMD=1, which is the whole game on a stock brcmfmac. The Makefile
# defaults it to 0 and there are three ways to build this, two of which silently do nothing:
#
#   default (ioctl)   __nex_driver_io: error ret=-1 errno=95   -- driver rejects the ioctl
#   -DUSE_NETLINK     nex_init_netlink: socket error (93)      -- needs a patched brcmfmac,
#                                                                 and *still exits 0*, so
#                                                                 csi-connected.sh reports
#                                                                 success having configured
#                                                                 nothing
#   USE_VENDOR_CMD=1  works
#
# Vendor commands travel over nl80211, which the stock driver does implement — which is what
# makes the whole no-patched-brcmfmac approach viable. Upstream says so in nexmon_csi
# discussion #395: "It is important that nexutil is compiled with USE_VENDOR_CMD=1 option
# though, otherwise IOCTLs will be rejected by the driver."
install_nexutil() {
    local csi_dir="$NEXMON_DIR/patches/$CHIP/$FW_VERSION/nexmon_csi"
    step "Building nexutil (vendor-command build)"

    # The fork carries an install-nexutil target that does this properly, including the clean.
    # Prefer it, so this stays correct if the build changes there, and fall back for a
    # checkout old enough not to have it.
    if grep -q '^install-nexutil:' "$csi_dir/Makefile.rpi" 2>/dev/null; then
        ( cd "$csi_dir" && set +u && . "$NEXMON_DIR/setup_env.sh" \
            && make -f Makefile.rpi install-nexutil ) > /dev/null 2>&1 \
            || die "make -f Makefile.rpi install-nexutil failed. It needs libnl-3-dev and
       libnl-genl-3-dev."
    else
        # The clean is not optional: object files from an earlier build keep their old
        # transport, so a vendor-command rebuild links the previous one and looks broken.
        ( cd "$NEXMON_DIR" && set +u && . ./setup_env.sh \
            && make -C utilities/libnexio clean > /dev/null 2>&1
          cd "$NEXMON_DIR" && set +u && . ./setup_env.sh \
            && make -C utilities/nexutil clean > /dev/null 2>&1
          cd "$NEXMON_DIR" && set +u && . ./setup_env.sh \
            && make -C utilities/nexutil install USE_VENDOR_CMD=1 ) > /dev/null 2>&1 \
            || die "could not build nexutil with USE_VENDOR_CMD=1. It needs libnl-3-dev and
       libnl-genl-3-dev; without a working nexutil, csi-connected.sh configures
       nothing and no CSI is ever emitted."
    fi

    command -v nexutil > /dev/null 2>&1 \
        || die "nexutil built but is not on PATH"
    # So the extractor can be configured without full root later if wanted.
    setcap cap_net_admin+ep "$(command -v nexutil)" 2>/dev/null || true

    # Prove it can actually reach the firmware rather than trusting the build. This is the
    # check whose absence let a non-functional nexutil look fine for an entire install.
    if nexutil -I"$IFACE" -k 2>&1 | grep -q "^chanspec:"; then
        ok "nexutil reaches the firmware"
    else
        warn "nexutil built but cannot read the chanspec from $IFACE. If $IFACE is not
    associated that is expected; otherwise CSI will not be emitted."
    fi
}

# ---------------------------------------------------------------------------------------
# Undoing the firmware
# ---------------------------------------------------------------------------------------

# The old undo here was `update-alternatives --auto brcmfmac43455-sdio.bin`, which could not
# work, in two separate ways. The patch registers the link group as *cyfmac43455-sdio.bin*
# (see install-firmware in Makefile.rpi), so that name matched nothing. And --auto selects the
# highest-priority alternative, which is nexmon's own at priority 30 and the only one
# registered — so even with the right name it would reinstall the patched firmware rather than
# remove it. Removing the alternative is what restores the packaged file.
#
# This is the command you need when the radio is dead and you are typing on a directly
# attached keyboard, so it must be correct.
FIRMWARE_ALT="cyfmac43455-sdio.bin"
FIRMWARE_PATCHED="/lib/firmware/nexmon/brcmfmac43455-sdio.bin"

RESTORE_FIRMWARE_SH=$(cat <<EOF
update-alternatives --remove $FIRMWARE_ALT $FIRMWARE_PATCHED >/dev/null 2>&1 || true
apt-get install --reinstall -y firmware-brcm80211 >/dev/null 2>&1 || \\
    echo "warning: could not reinstall firmware-brcm80211; do it by hand before rebooting"
EOF
)

restore_stock_firmware() {
    command -v update-alternatives > /dev/null 2>&1 || return 0
    step "Restoring the stock Wi-Fi firmware"
    update-alternatives --remove "$FIRMWARE_ALT" "$FIRMWARE_PATCHED" > /dev/null 2>&1 || true
    # --remove takes the symlink with it, so the packaged file has to come back from the
    # package rather than from anything left on disk.
    DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y firmware-brcm80211 \
        > /dev/null 2>&1 \
        || warn "could not reinstall firmware-brcm80211; run that by hand before rebooting"
    ok "stock firmware restored"
}

# ---------------------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------------------

detect_ap() {
    # The BSSID of the current association: the transmitter whose frames we want to measure.
    iw dev "$IFACE" link 2>/dev/null \
        | awk '/Connected to/ {print $3; exit}'
}

install_service() {
    step "Installing the forwarder"

    install -d "$PREFIX/bin"
    install -m 0755 "$SRC_DIR/csi_node.py" "$PREFIX/bin/csi_node.py"

    local csi_dir="$NEXMON_DIR/patches/$CHIP/$FW_VERSION/nexmon_csi"
    local connect="$csi_dir/utils/csi-connected.sh"
    [ -x "$connect" ] || [ -f "$connect" ] || die "missing $connect — was the fork cloned?"
    chmod +x "$connect" 2>/dev/null || true

    # Absolute path baked into the helpers and the env file: systemd units run with a minimal
    # PATH, and the control loop's scan needs `iw` by a path it can count on.
    local iw
    iw="$(command -v iw || true)"
    [ -n "$iw" ] || iw="/usr/sbin/iw"

    if [ -z "$AP_MAC" ]; then
        AP_MAC="$(detect_ap || true)"
    fi
    if [ -z "$AP_MAC" ]; then
        warn "$IFACE is not associated, so there is no access point to measure against.
    Connect it to Wi-Fi and run this again, or pass --ap <bssid>."
    else
        ok "measuring frames from $AP_MAC"
    fi

    case "$STIMULUS" in
        auto|always|off) ;;
        *) die "--stimulus must be auto, always or off (got '$STIMULUS')" ;;
    esac
    if [ "$STIMULUS" != off ] && [ ! -e "/sys/class/net/$STIMULUS_IFACE" ]; then
        # Not fatal — the wire may be plugged in later — but worth saying now, because the
        # failure is silent: the node emits nothing and simply measures whatever the household
        # happens to generate, which is the exact problem the stimulus exists to solve.
        warn "no interface '$STIMULUS_IFACE', so the Ethernet stimulus has nowhere to go.
    Pass --stimulus-iface <name>, or --stimulus off if this node has no wired link."
    elif [ "$STIMULUS" != off ]; then
        ok "stimulus on $STIMULUS_IFACE at $STIMULUS_HZ Hz ($STIMULUS)"
    fi

    if [ -z "$SERVER" ]; then
        SERVER="127.0.0.1"
    fi
    # Default the control URL to the server's HTTP port. When the server is on this Pi (the
    # one-command install) that is loopback; when it is elsewhere, the same host the frames go
    # to. Leaving CONTROL_URL empty disables the control loop, which is what an old server or a
    # deliberately read-only node wants.
    if [ -z "$CONTROL_URL" ]; then
        CONTROL_URL="http://$SERVER:$HTTP_PORT"
    fi

    # Retunes the capture to an explicit channel, or back to following the association.
    #
    # Not by `iw set channel`: on the BCM43455 that returns -95 (EOPNOTSUPP) for both `channel`
    # and `freq`, verified on a 3B+ running the patched firmware. The chip follows its
    # *association*, so a channel change means associating somewhere else and letting
    # csi-connected.sh read the resulting chanspec back out. That is the only sequence this
    # radio honours, and the earlier version of this file — which just called `iw` and hoped —
    # left the web UI's channel control silently doing nothing.
    cat > "$PREFIX/bin/tune.sh" <<'EOF'
#!/bin/sh
# Retune the capture to CH/BW (e.g. 36/80, 1/40), or 'auto' to follow the association.
# Written by pi/install-node.sh.
set -eu
. /etc/default/csi-node

IW="${CSI_IW:-iw}"
CHANNEL="${1:?tune.sh needs a channel like 36/80, or auto}"

[ "$CHANNEL" = auto ] && exec "$CSI_CAPTURE_UP_SH"

CH="${CHANNEL%%/*}"
if [ "$CH" -gt 14 ] 2>/dev/null; then BAND=a; else BAND=bg; fi

CON="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
    | awk -F: -v i="$CSI_IFACE" '$2==i {print $1; exit}')"
[ -n "$CON" ] || CON="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
    | awk -F: '$2=="802-11-wireless" {print $1; exit}')"
[ -n "$CON" ] || { echo "tune.sh: no wifi connection profile found" >&2; exit 1; }

# Scanning and association are both disabled while the extractor is armed.
"$CSI_CONNECT_SH" -i "$CSI_IFACE" --stop >/dev/null 2>&1 || true

SSID="$(nmcli -t -f 802-11-wireless.ssid connection show "$CON" | cut -d: -f2)"
nmcli device wifi rescan >/dev/null 2>&1 || true
sleep 5
# `nmcli -t` escapes the colons inside a BSSID as `\:`, so the field split leaves the
# backslashes behind. Reassembled and then unescaped: passing `78\:8C\:...` straight to
# `connection modify` is accepted quietly and pins nothing.
BSSID="$(nmcli -t -f SSID,BSSID,CHAN device wifi list 2>/dev/null \
    | awk -F: -v s="$SSID" -v c="$CH" '{n=NF; ch=$n; if ($1==s && ch==c) {
        mac=$2; for (i=3;i<n;i++) mac=mac":"$i; gsub(/\\/, "", mac); print mac; exit }}')"

# Pinning the BSSID stops the chip drifting back to the other band behind our back. Without a
# match, set the band and let NetworkManager choose.
nmcli connection modify "$CON" 802-11-wireless.band "$BAND" \
    802-11-wireless.bssid "${BSSID:-}" >/dev/null 2>&1 || true
nmcli connection up "$CON" >/dev/null 2>&1 || true

exec "$CSI_CAPTURE_UP_SH"
EOF
    chmod 0755 "$PREFIX/bin/tune.sh"

    cat > "$ENV_FILE" <<EOF
# Written by pi/install-node.sh. Edit and 'systemctl restart csi-node' to change.
CSI_SERVER_HOST=$SERVER
CSI_UDP_PORT=$PORT
CSI_NODE_ID=$NODE_ID
CSI_IFACE=$IFACE
CSI_PROBE_HZ=$PROBE_HZ
CSI_AP_MAC=$AP_MAC
CSI_STIMULUS=$STIMULUS
CSI_STIMULUS_IFACE=$STIMULUS_IFACE
CSI_STIMULUS_HZ=$STIMULUS_HZ
CSI_CONNECT_SH=$connect
# Control: the node polls this for channel and stimulus changes from the web UI, and reports
# what it applied. Blank it out to make this node read-only.
CSI_CONTROL_URL=$CONTROL_URL
CSI_TUNE_SH=$PREFIX/bin/tune.sh
CSI_CAPTURE_UP_SH=$PREFIX/bin/capture-up.sh
CSI_IW=$iw
EOF
    chmod 0644 "$ENV_FILE"

    # The extractor configuration does not survive a reboot or a reassociation, so it is set
    # up as part of starting the service rather than once at install time.
    #
    # Through wrapper scripts rather than inline in the unit, because systemd does its own
    # `$VAR` expansion in Exec lines and does not implement the shell's `${VAR:+...}` — an
    # optional argument written inline would silently come out wrong.
    cat > "$PREFIX/bin/capture-up.sh" <<'EOF'
#!/bin/sh
# Configure the nexmon extractor for the current association. Re-run on every service start:
# neither the chanspec nor the collection flag survives a reboot or a reassociation.
set -eu
. /etc/default/csi-node

IW="${CSI_IW:-iw}"

associated() {
    "$IW" dev "$CSI_IFACE" link 2>/dev/null | grep -q 'Connected to'
}

# Wait for the association rather than assuming it. Enabling CSI collection knocks the station
# off the access point, so on a restart the interface is usually still reconnecting — and with
# RestartSec=5 the service would otherwise spin in a loop that never lets NetworkManager
# finish, failing at ExecStartPre every time with "wlan0 is not associated".
if ! associated; then
    CON="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
        | awk -F: '$2=="802-11-wireless" {print $1; exit}')"
    [ -n "$CON" ] && nmcli connection up "$CON" >/dev/null 2>&1 || true
    i=0
    while [ $i -lt 20 ]; do
        associated && break
        sleep 2
        i=$((i + 1))
    done
fi

if [ -z "${CSI_AP_MAC:-}" ]; then
    # Not pinned at install time, so take whatever this interface is associated with now.
    CSI_AP_MAC="$("$IW" dev "$CSI_IFACE" link 2>/dev/null | awk '/Connected to/ {print $3; exit}')"
fi

if [ -n "${CSI_AP_MAC:-}" ]; then
    exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" -C 0x1 -N 0x1 -m "$CSI_AP_MAC"
fi

echo "csi-node: $CSI_IFACE is not associated; collecting without a transmitter filter" >&2
exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" -C 0x1 -N 0x1
EOF

    cat > "$PREFIX/bin/capture-down.sh" <<'EOF'
#!/bin/sh
# Stop collecting and re-enable scanning, so the Pi can roam normally again.
set -eu
. /etc/default/csi-node
exec "$CSI_CONNECT_SH" -i "$CSI_IFACE" --stop
EOF

    chmod 0755 "$PREFIX/bin/capture-up.sh" "$PREFIX/bin/capture-down.sh"

    cat > "$SERVICE" <<EOF
[Unit]
Description=WiFi CSI node (nexmon_csi capture and forward)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStartPre=$PREFIX/bin/capture-up.sh
ExecStart=/usr/bin/python3 $PREFIX/bin/csi_node.py
ExecStopPost=-$PREFIX/bin/capture-down.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    # A standalone undo, so that removing the node does not depend on still having a checkout
    # of this repo lying around. The top-level install.sh --uninstall calls it if it is there.
    cat > "$PREFIX/uninstall.sh" <<EOF
#!/bin/sh
# Remove the CSI node and put the stock Wi-Fi firmware back. Written by pi/install-node.sh.
set -eu
systemctl disable --now csi-node >/dev/null 2>&1 || true
[ -x "$PREFIX/bin/capture-down.sh" ] && "$PREFIX/bin/capture-down.sh" >/dev/null 2>&1 || true
rm -f "$SERVICE" "$ENV_FILE"
systemctl daemon-reload
$RESTORE_FIRMWARE_SH
echo "csi-node removed. Reboot to load the stock Wi-Fi firmware."
echo "The nexmon build is still in $PREFIX; remove it with: rm -rf $PREFIX $STATE_DIR"
EOF
    chmod 0755 "$PREFIX/uninstall.sh"

    systemctl daemon-reload
    systemctl enable csi-node > /dev/null 2>&1
    ok "csi-node.service installed"
}

start_service() {
    step "Starting the node"
    systemctl restart csi-node || true
    sleep 3
    if systemctl is-active --quiet csi-node; then
        ok "running"
    else
        warn "it did not stay up. The log says why:"
        say "    journalctl -u csi-node -n 40 --no-pager"
    fi
}

uninstall() {
    step "Removing the node"
    systemctl disable --now csi-node > /dev/null 2>&1 || true
    if [ -x "$PREFIX/bin/capture-down.sh" ]; then
        "$PREFIX/bin/capture-down.sh" > /dev/null 2>&1 || true
    fi
    rm -f "$SERVICE" "$ENV_FILE"
    systemctl daemon-reload
    ok "service removed"

    restore_stock_firmware
    say ""
    say "The nexmon build in $PREFIX was left alone; remove it with:"
    say "  sudo rm -rf $PREFIX $STATE_DIR"
    say "${bold}Reboot to load the stock firmware.${reset}"
}

summary() {
    say ""
    # This used to announce "The node is up." unconditionally, directly under start_service's
    # own warning that it had not stayed up. Saying it worked when it did not is worse than
    # saying nothing: it sends you to the app to look for frames that are never coming.
    if ! systemctl is-active --quiet csi-node 2>/dev/null; then
        warn "${bold}The node is installed but not running.${reset}"
        say "    journalctl -u csi-node -n 40 --no-pager     ${dim}(why it stopped)${reset}"
        say ""
        say "    The firmware is patched either way. If the radio itself is misbehaving:"
        say "      sudo $PREFIX/uninstall.sh && sudo reboot"
        say ""
        say "${dim}Config: $ENV_FILE${reset}"
        return 1
    fi

    say "${bold}The node is up.${reset}"
    say "  systemctl status csi-node"
    say "  journalctl -u csi-node -f     ${dim}(rate, subcarriers, RSSI every 10 s)${reset}"
    say ""
    say "It reports as node ${bold}$NODE_ID${reset} to ${SERVER}:${PORT}. Open the app and look at"
    say "Node health: a steady rate and sub-1% loss means the capture chain works."
    say ""
    say "${dim}Verify frames are actually being emitted, not just that the service runs:${reset}"
    say "  sudo tcpdump -i $IFACE dst port 5500 -c 5"
    say ""
    say "${dim}Config: $ENV_FILE${reset}"
}

main() {
    say "${bold}WiFi CSI${reset} — Raspberry Pi node"
    say ""

    if [ "$UNINSTALL" = 1 ]; then
        uninstall
        exit 0
    fi

    check_hardware

    if [ "$SKIP_BUILD" = 0 ]; then
        check_pagesize
        if stage_reached "built"; then
            ok "firmware already built; skipping (use --uninstall to start over)"
        else
            install_deps
            fetch_sources
            build_host_libs
            build_firmware
            stage_done "built"
        fi
    fi

    install_service
    start_service
    summary
}

main "$@"
