#!/bin/sh
set -eu

manifest="${1:-system-requirements.debian.txt}"
max_attempts=3
attempt=1

rewrite_debian_sources() {
    for source_file in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do
        [ -f "${source_file}" ] || continue
        sed -i "$@" "${source_file}"
    done
}

use_primary_debian_mirror() {
    rewrite_debian_sources 's|http://deb.debian.org|https://deb.debian.org|g'
}

use_official_fallback_mirrors() {
    # deb.debian.org is a CDN and can occasionally fail at one regional edge. Keep
    # recovery bounded to Debian-operated HTTPS endpoints; never silently switch to
    # an arbitrary third-party mirror or continue with an incomplete package index.
    rewrite_debian_sources \
        -e 's|https://deb.debian.org/debian-security|https://security.debian.org/debian-security|g' \
        -e 's|https://deb.debian.org/debian|https://ftp.debian.org/debian|g'
    echo "Primary Debian CDN unavailable; retrying with official Debian fallback mirrors." >&2
}

use_primary_debian_mirror

apt-get \
    -o Acquire::Retries=5 \
    -o Acquire::http::Timeout=60 \
    -o Acquire::https::Timeout=60 \
    update
while ! xargs --no-run-if-empty apt-get \
    -o Acquire::Retries=5 \
    -o Acquire::http::Timeout=60 \
    -o Acquire::https::Timeout=60 \
    install --yes --fix-missing --no-install-recommends < "${manifest}"
do
    if [ "${attempt}" -ge "${max_attempts}" ]; then
        echo "System dependency installation failed after ${max_attempts} attempts." >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    echo "System dependency installation failed; retrying attempt ${attempt}/${max_attempts}." >&2
    if [ "${attempt}" -eq 2 ]; then
        use_official_fallback_mirrors
    fi
    apt-get \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=60 \
        -o Acquire::https::Timeout=60 \
        update
done

rm -rf /var/lib/apt/lists/*
