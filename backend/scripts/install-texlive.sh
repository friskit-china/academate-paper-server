#!/bin/sh
set -eu

lock_file="${1:-texlive-runtime.lock}"
profile_file="${2:-texlive.profile}"

if [ ! -f "${lock_file}" ] || [ ! -f "${profile_file}" ]; then
    echo "TeX Live lock or profile file is missing." >&2
    exit 1
fi

# The lock is a version-controlled list of plain KEY=value assignments.
# shellcheck disable=SC1090
. "./${lock_file}"

for required_value in \
    TEXLIVE_RUNTIME_ID \
    TEXLIVE_RELEASE \
    TEXLIVE_REPOSITORY_URL \
    TEXLIVE_INSTALLER_URL \
    TEXLIVE_INSTALLER_SHA512 \
    TEXLIVE_TLPDB_SHA512
do
    eval "resolved_value=\${${required_value}:-}"
    if [ -z "${resolved_value}" ]; then
        echo "Missing ${required_value} in ${lock_file}." >&2
        exit 1
    fi
done

case "${TEXLIVE_RUNTIME_ID}:${TEXLIVE_RELEASE}" in
    arxiv-tl2025-2025-08-03:2025) ;;
    *)
        echo "Unsupported or inconsistent TeX Live runtime lock." >&2
        exit 1
        ;;
esac

temporary_dir="$(mktemp -d /tmp/academate-texlive.XXXXXX)"
trap 'rm -rf "${temporary_dir}"' EXIT HUP INT TERM

download() {
    output_path="$1"
    download_url="$2"
    curl \
        --fail \
        --location \
        --retry 5 \
        --retry-all-errors \
        --connect-timeout 30 \
        --max-time 600 \
        --silent \
        --show-error \
        --output "${output_path}" \
        "${download_url}"
}

installer_archive="${temporary_dir}/install-tl-unx.tar.gz"
download "${installer_archive}" "${TEXLIVE_INSTALLER_URL}"
printf '%s  %s\n' "${TEXLIVE_INSTALLER_SHA512}" "${installer_archive}" \
    | sha512sum --check --strict -

repository_checksum_file="${temporary_dir}/texlive.tlpdb.sha512"
download \
    "${repository_checksum_file}" \
    "${TEXLIVE_REPOSITORY_URL}/tlpkg/texlive.tlpdb.sha512"
actual_tlpdb_sha512="$(awk 'NR == 1 { print $1 }' "${repository_checksum_file}")"
if [ "${actual_tlpdb_sha512}" != "${TEXLIVE_TLPDB_SHA512}" ]; then
    echo "TeX Live repository database checksum does not match the lock file." >&2
    exit 1
fi

tar --extract --gzip --file "${installer_archive}" --directory "${temporary_dir}"
installer_path="$(find "${temporary_dir}" -mindepth 2 -maxdepth 2 -type f -name install-tl -print -quit)"
if [ -z "${installer_path}" ]; then
    echo "The verified TeX Live archive did not contain install-tl." >&2
    exit 1
fi

platform="$(perl "${installer_path}" --print-platform)"
local_repository="${temporary_dir}/tlnet"
mkdir -p "${local_repository}/archive" "${local_repository}/tlpkg"
package_database="${local_repository}/tlpkg/texlive.tlpdb"
download "${package_database}" "${TEXLIVE_REPOSITORY_URL}/tlpkg/texlive.tlpdb"
printf '%s  %s\n' "${TEXLIVE_TLPDB_SHA512}" "${package_database}" \
    | sha512sum --check --strict -
cp "${repository_checksum_file}" "${local_repository}/tlpkg/texlive.tlpdb.sha512"

download_list="${temporary_dir}/texlive-archives.txt"
python scripts/prepare_texlive_repository.py \
    --tlpdb "${package_database}" \
    --repository-url "${TEXLIVE_REPOSITORY_URL}" \
    --platform "${platform}" \
    --scheme scheme-full \
    --output "${download_list}"
aria2c \
    --input-file="${download_list}" \
    --dir="${local_repository}/archive" \
    --user-agent=curl/7.88.1 \
    --http-accept-gzip=false \
    --max-concurrent-downloads=12 \
    --max-connection-per-server=1 \
    --split=1 \
    --continue=true \
    --max-tries=5 \
    --retry-wait=2 \
    --connect-timeout=30 \
    --timeout=120 \
    --check-certificate=true \
    --auto-file-renaming=false \
    --file-allocation=none \
    --console-log-level=warn \
    --summary-interval=30 \
    --download-result=hide

TEXLIVE_INSTALL_NO_WELCOME=1 perl "${installer_path}" \
    --profile "${profile_file}" \
    --repository "${local_repository}" \
    --no-interaction \
    --no-persistent-downloads \
    --no-continue \
    --no-doc-install \
    --no-src-install

binary_dir="$(find "/opt/texlive/${TEXLIVE_RELEASE}/bin" -mindepth 1 -maxdepth 1 -type d -print -quit)"
if [ -z "${binary_dir}" ] || [ ! -x "${binary_dir}/kpsewhich" ]; then
    echo "TeX Live did not install a usable binary directory." >&2
    exit 1
fi

ln -s "${binary_dir}" /opt/texlive/bin
printf '%s\n' \
    "runtime_id=${TEXLIVE_RUNTIME_ID}" \
    "release=${TEXLIVE_RELEASE}" \
    "repository=${TEXLIVE_REPOSITORY_URL}" \
    "tlpdb_sha512=${TEXLIVE_TLPDB_SHA512}" \
    > /opt/texlive/runtime-profile

/opt/texlive/bin/tex --version | grep -F "TeX Live ${TEXLIVE_RELEASE}"
/opt/texlive/bin/kpsewhich dsfont.sty | grep -F '/dsfont.sty'
/opt/texlive/bin/kpsewhich extarrows.sty | grep -F '/extarrows.sty'
/opt/texlive/bin/kpsewhich tikz-cd.sty | grep -F '/tikz-cd.sty'
