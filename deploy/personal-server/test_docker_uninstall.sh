#!/bin/sh
# Integration with a local Docker daemon; only create/remove uniquely named test resources.
set -eu
script=$(cd "$(dirname "$0")/../.." && pwd)/amp
# Help is available even when the host has no Docker CLI or running installation.
for flag in -h --help; do
    PATH=/nonexistent "$script" "$flag" | grep -F './amp uninstall' >/dev/null
    PATH=/nonexistent "$script" uninstall "$flag" | grep -F yes >/dev/null
done
PATH=/nonexistent "$script" help uninstall | grep -F yes >/dev/null
tmp=$(mktemp -d)
name="amp-uninstall-qa-$(basename "$tmp" | tr '[:upper:]' '[:lower:]')"
ref="$name:qa"
volume="$name-data"
real_docker=$(command -v docker)
cleanup() {
    for item in "$name" "$name-helper"; do docker rm -f "$item" >/dev/null 2>&1 || :; done
    docker volume rm "$volume" "academate-paper-uninstall-$name" >/dev/null 2>&1 || :
    docker network rm "${name}_default" >/dev/null 2>&1 || :
    docker image rm "$ref" >/dev/null 2>&1 || :
    rm -rf "$tmp"
}
trap cleanup EXIT
cat >"$tmp/Dockerfile" <<'FILE'
FROM scratch
LABEL org.opencontainers.image.title="AcadeMate Paper Server"
CMD ["/unused"]
FILE
docker build -q --label "qa.instance=$name" -t "$ref" "$tmp" >/dev/null
docker volume create "$volume" >/dev/null
docker create --name "$name" --mount "type=volume,src=$volume,dst=/data" "$ref" >/dev/null
for reply in '' no YES; do
    printf '%s\n' "$reply" | "$script" uninstall "$name" >/dev/null
    docker inspect "$name" >/dev/null
done
"$script" uninstall "$name" </dev/null >/dev/null
if docker volume inspect "academate-paper-uninstall-$name" >/dev/null 2>&1; then exit 1; fi
# A user's existing volume with the journal name cannot serve as an uninstall record.
docker volume create "academate-paper-uninstall-$name" >/dev/null
if printf 'yes\n' | "$script" uninstall "$name" >/dev/null 2>&1; then exit 1; fi
docker inspect "$name" >/dev/null
docker volume inspect "academate-paper-uninstall-$name" >/dev/null
docker volume rm "academate-paper-uninstall-$name" >/dev/null

# Even a stopped second container can own the data. Sharing just the image is allowed.
docker create --name "$name-helper" --mount "type=volume,src=$volume,dst=/other" "$ref" >/dev/null
if printf 'yes\n' | "$script" uninstall "$name" >/dev/null 2>&1; then exit 1; fi
docker inspect "$name" >/dev/null
docker rm "$name-helper" >/dev/null

# A bind mount is a host directory; confirm that we can never recursively remove it.
docker create --name "$name-helper" --mount "type=bind,src=$tmp,dst=/data" "$ref" >/dev/null
if printf 'yes\n' | "$script" uninstall "$name-helper" >/dev/null 2>&1; then exit 1; fi
test -f "$tmp/Dockerfile"
docker inspect "$name-helper" >/dev/null

# Inject a single error between container and volume deletion, then retry the same command.
mkdir "$tmp/bin"
cat >"$tmp/bin/docker" <<'SH'
#!/bin/sh
if [ "$1" = volume ] && [ "$2" = rm ] && [ "$3" = "$QA_VOLUME" ]; then exit 1; fi
exec "$QA_DOCKER" "$@"
SH
chmod +x "$tmp/bin/docker"
if printf 'yes\n' | QA_VOLUME="$volume" QA_DOCKER="$real_docker" PATH="$tmp/bin:$PATH" "$script" uninstall "$name" >/dev/null 2>&1; then exit 1; fi
if docker inspect "$name" >/dev/null 2>&1; then exit 1; fi
docker volume inspect "$volume" >/dev/null
docker volume inspect "academate-paper-uninstall-$name" >/dev/null
printf 'yes\n' | "$script" uninstall "$name" >/dev/null
docker inspect "$name-helper" >/dev/null
docker image inspect "$ref" >/dev/null
if docker volume inspect "$volume" >/dev/null 2>&1; then exit 1; fi
if docker volume inspect "academate-paper-uninstall-$name" >/dev/null 2>&1; then exit 1; fi

# A Compose project can share its named default network with a stopped container.
docker rm "$name-helper" >/dev/null
docker network create --label "com.docker.compose.project=$name" --label com.docker.compose.network=default "${name}_default" >/dev/null
create_compose_container() {
    docker create --label "com.docker.compose.project=$name" --network "${name}_default" \
        --name "$name" --mount "type=volume,src=$volume,dst=/data" "$ref" >/dev/null
}
docker create --network "${name}_default" --name "$name-helper" "$ref" >/dev/null
create_compose_container
printf 'yes\n' | "$script" uninstall "$name" >/dev/null
docker network inspect "${name}_default" >/dev/null
docker rm "$name-helper" >/dev/null

# Repeat with an exclusive image and network. Both must disappear along with the volume.
create_compose_container
printf 'yes\n' | "$script" uninstall "$name" >/dev/null
if docker image inspect "$ref" >/dev/null 2>&1; then exit 1; fi
if docker network inspect "${name}_default" >/dev/null 2>&1; then exit 1; fi
echo 'Docker uninstall integration passed.'
