#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $# -gt 1 ]]; then
  echo "usage: sudo ./packaging/install.sh [extracted-package-directory]" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then package_dir="$(cd -- "$1" && pwd -P)"; fi
if [[ "$(id -u)" -ne 0 ]]; then echo "rootで実行してください" >&2; exit 1; fi

for command in python3.13 systemctl getent groupadd useradd install sha256sum cmp runuser hostname mv busctl timedatectl stat apt-get id flock; do
  command -v "$command" >/dev/null || { echo "必要なcommandがありません: $command" >&2; exit 1; }
done
[[ -f "$package_dir/SHA256SUMS" ]] || { echo "SHA256SUMSがありません" >&2; exit 1; }
python3.13 - "$package_dir" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = root / "SHA256SUMS"
if root.is_symlink() or not root.is_dir() or manifest.is_symlink() or not manifest.is_file():
    raise SystemExit("展開directoryまたはSHA256SUMSが通常fileではありません")
actual = set()
for current, directories, filenames in os.walk(root, followlinks=False):
    base = Path(current)
    for name in directories:
        if not stat.S_ISDIR((base / name).lstat().st_mode):
            raise SystemExit("package treeにsymlinkまたは特殊directoryがあります")
    for name in filenames:
        path = base / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("package treeにsymlinkまたは特殊fileがあります")
        relative = path.relative_to(root).as_posix()
        if relative != "SHA256SUMS":
            actual.add(relative)
if manifest.stat().st_size > 4 * 1024 * 1024:
    raise SystemExit("SHA256SUMSが大きすぎます")
listed = set()
for line in manifest.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
    if match is None:
        raise SystemExit("SHA256SUMSの形式が不正です")
    digest, name = match.groups()
    if name == "SHA256SUMS" or name.startswith("/") or "\\" in name:
        raise SystemExit("SHA256SUMSに不正なpathがあります")
    if any(part in {"", ".", ".."} for part in name.split("/")) or name in listed:
        raise SystemExit("SHA256SUMSに重複または不正なpathがあります")
    listed.add(name)
if listed != actual:
    raise SystemExit("SHA256SUMSとpackage file listが一致しません")
PY
(cd -- "$package_dir" && sha256sum --check --strict SHA256SUMS)
python3.13 - <<'PY'
import platform
import shlex
import sys
from pathlib import Path

release = {}
for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        release[key] = shlex.split(value)[0]
if sys.version_info[:2] != (3, 13) or platform.machine() != "aarch64":
    raise SystemExit("Python 3.13/aarch64が必要です")
if release.get("VERSION_CODENAME") != "trixie":
    raise SystemExit("Raspberry Pi OS 64-bit / Debian Trixieが必要です")
PY
project_version="$(python3.13 - "$package_dir/pyproject.toml" <<'PY'
import sys
import tomllib
from pathlib import Path
print(tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]["version"])
PY
)"
[[ "$project_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$ ]] || {
  echo "versionが不正です" >&2; exit 1;
}

set_tree_owner_modes() {
  python3.13 - "$1" "$2" "$3" "$4" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
uid, gid = int(sys.argv[2]), int(sys.argv[3])
writable = sys.argv[4] == "writable"
directories = []
files = []
for current, names, filenames in os.walk(root, topdown=True, followlinks=False):
    directory = Path(current)
    directories.append(directory)
    for name in tuple(names):
        path = directory / name
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise SystemExit("release treeにsymlinkまたは特殊directoryがあります")
    for name in filenames:
        path = directory / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("release treeにsymlinkまたは特殊fileがあります")
        files.append((path, info.st_mode))
for path in reversed(directories):
    os.chown(path, uid, gid, follow_symlinks=False)
    path.chmod(0o750 if writable else 0o755)
for path, mode in files:
    os.chown(path, uid, gid, follow_symlinks=False)
    path.chmod((0o640 if writable else 0o644) | (0o111 if mode & 0o111 else 0))
PY
}
run_build_user() {
  runuser -u raspi-recorder-update -- env -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 HOME=/nonexistent \
    PIP_CONFIG_FILE=/dev/null PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 \
    PYTHONDONTWRITEBYTECODE=1 "$@"
}
[[ "$(basename -- "$package_dir")" == "raspi-recorder-$project_version" ]] || {
  echo "展開directory名と製品versionが一致しません" >&2; exit 1;
}

opt_root=/opt/raspi-recorder
release_root="$opt_root/releases"
release_path="$release_root/$project_version"
state_dir=/var/lib/raspi-recorder
current="$opt_root/current"
assert_no_symlink_path() {
  local path="$1"
  local current=/ part
  local -a components=()
  IFS=/ read -r -a components <<< "${path#/}"
  for part in "${components[@]}"; do
    [[ -n "$part" ]] || continue
    current="${current%/}/$part"
    if [[ -L "$current" ]]; then
      echo "symlinkを含むpathは変更しません: $current" >&2
      exit 1
    fi
  done
}
assert_no_symlink_path "$opt_root"
assert_no_symlink_path "$release_root"
assert_no_symlink_path "$release_path"
assert_no_symlink_path "$state_dir"
assert_no_symlink_path /etc/systemd/system/raspi-recorder-engine.service.d
assert_no_symlink_path /etc/systemd/system/raspi-recorder-web.service.d
assert_no_symlink_path /etc/systemd/system/raspi-recorder-clock.socket
assert_no_symlink_path /etc/systemd/system/raspi-recorder-clock@.service
assert_no_symlink_path /etc/systemd/system/raspi-recorder-update.socket
assert_no_symlink_path /etc/systemd/system/raspi-recorder-update@.service
assert_no_symlink_path /etc/systemd/system/raspi-recorder-update-recover.service
assert_no_symlink_path /usr/lib/raspi-recorder/update-recover
assert_no_symlink_path /var/lib/raspi-recorder-update
assert_no_symlink_path /run/raspi-recorder-update/update-helper.lock
previous_release=""
if [[ -e "$current" ]]; then
  if [[ ! -L "$current" ]]; then
    echo "currentがsymlinkではありません。既存pathを保持します" >&2; exit 1
  fi
  previous_target="$(readlink -- "$current")"
  if [[ ! "$previous_target" =~ ^releases/([0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?)$ ]]; then
    echo "currentがversioned releaseを指していません。既存pathを保持します" >&2; exit 1
  fi
  previous_release="$opt_root/$previous_target"
  assert_no_symlink_path "$previous_release"
  if [[ ! -d "$previous_release" ]]; then
    echo "currentの参照先releaseがありません。既存pathを保持します" >&2; exit 1
  fi
fi
# Share the root helper's lock before changing dependencies, releases or units.
install -d -o root -g root -m 0755 /run/raspi-recorder-update
exec 9>/run/raspi-recorder-update/update-helper.lock
chmod 0600 /run/raspi-recorder-update/update-helper.lock
flock -n 9 || { echo "別のinstall/updateが実行中です" >&2; exit 1; }
privileged_state_dir=/var/lib/raspi-recorder-update
if [[ -e "$privileged_state_dir" ]] && [[ ! -d "$privileged_state_dir" || -L "$privileged_state_dir" || "$(stat -c '%u:%g:%a' -- "$privileged_state_dir")" != "0:0:700" ]]; then
  echo "update helper state directory must remain root:root mode 0700" >&2
  exit 1
fi
getent group video >/dev/null || { echo "video groupがありません" >&2; exit 1; }
unit_replacements=()
check_managed_unit() {
  local unit="$1"
  local source="$package_dir/packaging/systemd/$unit"
  local target="/etc/systemd/system/$unit"
  local previous_unit=""
  [[ -f "$source" && ! -L "$source" ]] || { echo "package unitがありません: $source" >&2; exit 1; }
  if [[ -L "$target" ]]; then
    echo "既存unitがsymlinkです。上書きしません: $target" >&2; exit 1
  fi
  if [[ -e "$target" ]]; then
    [[ -f "$target" ]] || { echo "既存unitが通常fileではありません: $target" >&2; exit 1; }
    if ! cmp -s "$source" "$target"; then
      previous_unit="$previous_release/packaging/systemd/$unit"
      if [[ -n "$previous_release" && -f "$previous_unit" && ! -L "$previous_unit" ]] && cmp -s "$previous_unit" "$target"; then
        unit_replacements+=("$unit")
      else
        echo "既存unitを上書きしません: $target" >&2; exit 1
      fi
    fi
  fi
}
for unit in raspi-recorder-engine.service raspi-recorder-web.service raspi-recorder-clock.socket 'raspi-recorder-clock@.service'; do
  check_managed_unit "$unit"
done
for unit in raspi-recorder-update.socket 'raspi-recorder-update@.service' raspi-recorder-update-recover.service; do
  check_managed_unit "$unit"
done
if [[ -n "${unit_replacements[*]-}" ]]; then
  active_units="$(systemctl list-units --state=active --plain --no-legend 'raspi-recorder-*')"
  if [[ -n "$active_units" ]]; then
    echo "既存serviceまたはsocketが稼働中です。停止してからunitを更新してください" >&2; exit 1
  fi
fi
if [[ -e "$current" && ! -L "$current" ]]; then
  echo "currentがsymlinkではありません。既存pathを保持します" >&2; exit 1
fi
if [[ -L "$release_path" || ( -e "$release_path" && ! -d "$release_path" ) ]]; then
  echo "version pathが不正です: $release_path" >&2; exit 1
fi

groups=(video raspi-recorder-clock)
legacy_groups=(video)
for group in audio; do
  if getent group "$group" >/dev/null; then
    groups+=("$group")
    legacy_groups+=("$group")
  fi
done
dropin_dir=/etc/systemd/system/raspi-recorder-engine.service.d
dropin="$dropin_dir/10-hardware-groups.conf"
expected_dropin="$(printf '[Service]\nSupplementaryGroups=\nSupplementaryGroups=%s' "${groups[*]}")"
legacy_dropin="$(printf '[Service]\nSupplementaryGroups=\nSupplementaryGroups=%s' "${legacy_groups[*]}")"
replace_dropin=0
if [[ -L "$dropin" ]]; then
  echo "既存hardware group overrideがsymlinkです。上書きしません" >&2; exit 1
fi
if [[ -e "$dropin" ]]; then
  existing_dropin="$(cat -- "$dropin")"
  if [[ "$existing_dropin" == "$legacy_dropin" ]]; then
    replace_dropin=1
  elif [[ "$existing_dropin" != "$expected_dropin" ]]; then
    echo "既存hardware group overrideを上書きしません" >&2; exit 1
  fi
fi
clock_dropin_dir=/etc/systemd/system/raspi-recorder-engine.service.d
clock_dropin_source="$package_dir/packaging/systemd/raspi-recorder-engine.service.d/20-clock-helper.conf"
clock_dropin="$clock_dropin_dir/20-clock-helper.conf"
replace_clock_dropin=0
[[ -f "$clock_dropin_source" ]] || { echo "clock helper drop-inがありません" >&2; exit 1; }
if [[ -L "$clock_dropin" ]]; then
  echo "既存clock helper drop-inがsymlinkです。上書きしません" >&2; exit 1
fi
if [[ -e "$clock_dropin" ]] && { [[ ! -f "$clock_dropin" ]] || ! cmp -s "$clock_dropin_source" "$clock_dropin"; }; then
  previous_clock_dropin="$previous_release/packaging/systemd/raspi-recorder-engine.service.d/20-clock-helper.conf"
  if [[ -n "$previous_release" && -f "$previous_clock_dropin" && ! -L "$previous_clock_dropin" ]] && cmp -s "$previous_clock_dropin" "$clock_dropin"; then
    replace_clock_dropin=1
  else
    echo "既存clock helper drop-inを上書きしません: $clock_dropin" >&2; exit 1
  fi
fi
update_dropin_source="$package_dir/packaging/systemd/raspi-recorder-engine.service.d/30-update-helper.conf"
update_dropin="$clock_dropin_dir/30-update-helper.conf"
replace_update_dropin=0
[[ -f "$update_dropin_source" ]] || { echo "update helper drop-inがありません" >&2; exit 1; }
if [[ -L "$update_dropin" ]]; then
  echo "既存update helper drop-inがsymlinkです。上書きしません" >&2; exit 1
fi
if [[ -e "$update_dropin" ]] && { [[ ! -f "$update_dropin" ]] || ! cmp -s "$update_dropin_source" "$update_dropin"; }; then
  previous_update_dropin="$previous_release/packaging/systemd/raspi-recorder-engine.service.d/30-update-helper.conf"
  if [[ -n "$previous_release" && -f "$previous_update_dropin" && ! -L "$previous_update_dropin" ]] && cmp -s "$previous_update_dropin" "$update_dropin"; then
    replace_update_dropin=1
  else
    echo "既存update helper drop-inを上書きしません: $update_dropin" >&2; exit 1
  fi
fi
recover_source="$package_dir/packaging/update-recover.py"
recover_target=/usr/lib/raspi-recorder/update-recover
replace_recover=0
[[ -f "$recover_source" ]] || { echo "update recovery launcherがありません" >&2; exit 1; }
if [[ -L "$recover_target" ]]; then
  echo "既存update recovery launcherがsymlinkです。上書きしません" >&2; exit 1
fi
if [[ -e "$recover_target" ]] && { [[ ! -f "$recover_target" ]] || ! cmp -s "$recover_source" "$recover_target"; }; then
  previous_recover="$previous_release/packaging/update-recover.py"
  if [[ -n "$previous_release" && -f "$previous_recover" && ! -L "$previous_recover" ]] && cmp -s "$previous_recover" "$recover_target"; then
    replace_recover=1
  else
    echo "既存update recovery launcherを上書きしません: $recover_target" >&2; exit 1
  fi
fi
short_hostname="$(hostname -s)"
[[ "$short_hostname" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || {
  echo "Pi hostnameを安全に検証できません" >&2; exit 1;
}
web_dropin_dir=/etc/systemd/system/raspi-recorder-web.service.d
web_dropin="$web_dropin_dir/10-trusted-hosts.conf"
expected_web_dropin="$(printf '[Service]\nEnvironment=RASPI_RECORDER_TRUSTED_HOSTS=%s,%s.local,localhost' "$short_hostname" "$short_hostname")"
if [[ -L "$web_dropin" ]]; then
  echo "既存Web trusted-host overrideがsymlinkです。上書きしません" >&2; exit 1
fi
replace_web_dropin=0
if [[ -e "$web_dropin" ]] && [[ "$(cat -- "$web_dropin")" != "$expected_web_dropin" ]]; then
  existing_web_dropin="$(cat -- "$web_dropin")"
  if [[ "$existing_web_dropin" =~ ^\[Service\]$'\n'Environment=RASPI_RECORDER_TRUSTED_HOSTS=[A-Za-z0-9][A-Za-z0-9.-]*,[A-Za-z0-9][A-Za-z0-9.-]*\.local,localhost$ ]]; then
    # installerが旧hostnameで生成したfileは、hostname変更後も管理対象として差し替える。
    replace_web_dropin=1
  else
    echo "既存Web trusted-host overrideを上書きしません" >&2; exit 1
  fi
fi
if (( replace_clock_dropin || replace_update_dropin || replace_recover )); then
  active_units="$(systemctl list-units --state=active --plain --no-legend 'raspi-recorder-*')"
  if [[ -n "$active_units" ]]; then
    echo "既存serviceまたはsocketが稼働中です。停止してからdrop-in／recovery launcherを更新してください" >&2; exit 1
  fi
fi

apt-get update
apt-get install -y --no-install-recommends \
  python3.13-dev python3.13-venv build-essential pkg-config zstd ffmpeg \
  libavcodec-dev libavdevice-dev libavfilter-dev libavformat-dev libavutil-dev \
  libswresample-dev libswscale-dev libjpeg-dev zlib1g-dev \
  libtiff-dev libfreetype6-dev liblcms2-dev libwebp-dev libopenjp2-7-dev \
  libharfbuzz-dev libfribidi-dev libxcb1-dev

getent group raspi-recorder >/dev/null || groupadd --system raspi-recorder
getent group raspi-recorder-clock >/dev/null || groupadd --system raspi-recorder-clock
getent passwd raspi-recorder >/dev/null || useradd --system --gid raspi-recorder --home-dir /var/lib/raspi-recorder --no-create-home --shell /usr/sbin/nologin raspi-recorder
getent passwd raspi-recorder-web >/dev/null || useradd --system --gid raspi-recorder --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin raspi-recorder-web
getent group raspi-recorder-update >/dev/null || groupadd --system raspi-recorder-update
getent passwd raspi-recorder-update >/dev/null || useradd --system --gid raspi-recorder-update --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin raspi-recorder-update
[[ "$(id -gn raspi-recorder-update)" == "raspi-recorder-update" ]] || {
  echo "update build userのprimary groupが不正です" >&2; exit 1;
}
check_service_user() {
  local user="$1" primary_group="$2"
  if [[ "$(id -gn "$user")" != "$primary_group" ]] || \
     [[ "$(getent passwd "$user" | cut -d: -f7)" != "/usr/sbin/nologin" ]]; then
    echo "既存service userのprimary groupまたはshellが不正です: $user" >&2; exit 1
  fi
}
check_service_user raspi-recorder raspi-recorder
check_service_user raspi-recorder-web raspi-recorder
check_service_user raspi-recorder-update raspi-recorder-update
command -v zstd >/dev/null || { echo "zstd commandがありません" >&2; exit 1; }
mkdir -p "$release_root"
if [[ -d "$release_path" ]]; then
  (cd -- "$release_path" && sha256sum --check --strict SHA256SUMS)
  cmp -s "$package_dir/SHA256SUMS" "$release_path/SHA256SUMS" || {
    echo "同versionの内容が異なるため既存releaseを保持して停止します" >&2; exit 1;
  }
  [[ -d "$release_path/.venv" && ! -L "$release_path/.venv" && \
     -x "$release_path/.venv/bin/raspi-recorder" ]] || {
    echo "同versionのreleaseに実行可能な専用venvがありません。既存を保持して停止します" >&2
    exit 1
  }
  # 内容が一致しても所有者・modeが改変されている可能性があるため、
  # 新規導入時と同じ不变化を再利用時にも適用する。
  set_tree_owner_modes "$release_path" 0 0 immutable
  run_build_user "$release_path/.venv/bin/raspi-recorder" --version
  run_build_user "$release_path/.venv/bin/python" -c 'import av, flask, PIL; print("runtime imports ok", av.__version__)'
else
  stage="$release_root/.install-$project_version-$$"
  [[ ! -e "$stage" && ! -L "$stage" ]] || { echo "staging pathが既にあります" >&2; exit 1; }
  mkdir -m 0755 "$stage"
  cleanup_install() {
    rm -rf -- "$stage"
  }
  trap cleanup_install EXIT
  cp -a -- "$package_dir/." "$stage/"
  set_tree_owner_modes "$stage" "$(id -u raspi-recorder-update)" "$(id -g raspi-recorder-update)" writable
  run_build_user python3.13 -m venv --copies "$stage/.venv"
  # Debian's venv creates a redundant lib64 -> lib link; packages use lib/.
  venv_lib64="$stage/.venv/lib64"
  if [[ -L "$venv_lib64" ]]; then
    [[ "$(readlink -- "$venv_lib64")" == "lib" ]] || {
      echo "Python venvのlib64 symlinkが想定外です" >&2; exit 1;
    }
    rm -- "$venv_lib64"
  fi
  run_build_user "$stage/.venv/bin/python" -m pip install \
      --disable-pip-version-check --no-cache-dir --no-input --no-compile \
      --no-binary=av,blinker,click,flask,gunicorn,itsdangerous,jinja2,markupsafe,pillow,werkzeug \
      --index-url https://pypi.org/simple "$stage"
  run_build_user "$stage/.venv/bin/raspi-recorder" --version
  run_build_user "$stage/.venv/bin/python" -c 'import av, flask, PIL; print("runtime imports ok", av.__version__)'
  run_build_user python3.13 - "$stage/.venv" "$stage" "$release_path" <<'PY'
import shutil
import stat
import sys
from pathlib import Path

venv = Path(sys.argv[1])
old_root = Path(sys.argv[2]).as_posix().encode()
new_root = Path(sys.argv[3]).as_posix().encode()
cache_dirs = sorted(
    venv.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True
)
for cache in cache_dirs:
    if cache.is_symlink() or not cache.is_dir():
        raise SystemExit("venvに不正なbytecode cache pathがあります")
    shutil.rmtree(cache)

text_files = [venv / "pyvenv.cfg"]
text_files.extend(path for path in (venv / "bin").iterdir() if path.is_file())
text_files.extend(
    path
    for path in venv.rglob("*")
    if path.name == "direct_url.json" or path.suffix in {".cfg", ".json", ".pth"}
)
changed = 0
for path in set(text_files):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("venv relocation対象にsymlinkまたは特殊fileがあります")
    content = path.read_bytes()
    if old_root in content:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit("venvのbinary fileにstaging pathがあります") from exc
        path.write_bytes(text.replace(old_root.decode(), new_root.decode()).encode())
        path.chmod(stat.S_IMODE(info.st_mode))
        changed += 1
for path in set(text_files):
    if old_root in path.read_bytes():
        raise SystemExit("venvにstaging pathが残っています")
print(f"relocated venv paths in {changed} text files")
PY
  set_tree_owner_modes "$stage" 0 0 immutable
  mv -T -n -- "$stage" "$release_path"
  if [[ -e "$stage" || ! -d "$release_path" || -L "$release_path" ]]; then
    echo "別のreleaseが同時に作られたためversion pathを変更せず停止します" >&2
    exit 1
  fi
  if ! run_build_user "$release_path/.venv/bin/raspi-recorder" --version || \
     ! run_build_user "$release_path/.venv/bin/python" -c 'import av, flask, PIL; print("runtime imports ok", av.__version__)'; then
    mv -T -- "$release_path" "$stage"
    echo "release移設後のCLI/import検証に失敗しました" >&2
    exit 1
  fi
  trap - EXIT
fi

if [[ ! -e "$state_dir" ]]; then
  install -d -o raspi-recorder -g raspi-recorder -m 2770 "$state_dir"
elif [[ ! -d "$state_dir" || -L "$state_dir" ]] \
  || [[ "$(stat -c '%u:%g' -- "$state_dir")" != "$(id -u raspi-recorder):$(id -g raspi-recorder)" ]] \
  || (( (8#$(stat -c '%a' -- "$state_dir") & 0007) != 0 )) \
  || ! runuser -u raspi-recorder -- test -w "$state_dir"; then
  echo "既存state directoryは変更せず停止します。pathと権限を確認してください" >&2
  exit 1
fi
if [[ ! -e "$privileged_state_dir" ]]; then
  install -d -o root -g root -m 0700 "$privileged_state_dir"
fi
# 長時間のAPT／source buildを挟んだため、書込み直前にsymlink検査をやり直す。
assert_no_symlink_path /etc/systemd/system/raspi-recorder-engine.service.d
assert_no_symlink_path /etc/systemd/system/raspi-recorder-web.service.d
assert_no_symlink_path /etc/systemd/system/raspi-recorder-clock.socket
assert_no_symlink_path /etc/systemd/system/raspi-recorder-clock@.service
assert_no_symlink_path /etc/systemd/system/raspi-recorder-update.socket
assert_no_symlink_path /etc/systemd/system/raspi-recorder-update@.service
assert_no_symlink_path /etc/systemd/system/raspi-recorder-update-recover.service
assert_no_symlink_path /usr/lib/raspi-recorder/update-recover
assert_no_symlink_path /var/lib/raspi-recorder-update
install_managed_unit() {
  local unit="$1"
  local source="$release_path/packaging/systemd/$unit"
  local target="/etc/systemd/system/$unit"
  local previous_unit=""
  local temp=""
  local replace=0
  local replacements=" ${unit_replacements[*]-} "
  case "$replacements" in *" $unit "*) replace=1 ;; esac
  if [[ ! -e "$target" ]]; then
    install -o root -g root -m 0644 "$source" "$target"
  elif ((replace)); then
    previous_unit="$previous_release/packaging/systemd/$unit"
    if cmp -s "$source" "$target"; then return; fi
    if [[ -z "$previous_release" || ! -f "$previous_unit" || -L "$previous_unit" ]] || ! cmp -s "$previous_unit" "$target"; then
      echo "unitが検証後に変更されました。上書きしません: $target" >&2; exit 1
    fi
    active_units="$(systemctl list-units --state=active --plain --no-legend 'raspi-recorder-*')"
    if [[ -n "$active_units" ]]; then
      echo "既存serviceまたはsocketが稼働中です。unitを更新しません" >&2; exit 1
    fi
    temp="$target.tmp.$$"
    [[ ! -e "$temp" && ! -L "$temp" ]] || { echo "unit staging pathが既にあります" >&2; exit 1; }
    install -o root -g root -m 0644 "$source" "$temp"
    mv -Tf -- "$temp" "$target"
  fi
}
for unit in raspi-recorder-engine.service raspi-recorder-web.service raspi-recorder-clock.socket 'raspi-recorder-clock@.service' raspi-recorder-update.socket 'raspi-recorder-update@.service' raspi-recorder-update-recover.service; do
  install_managed_unit "$unit"
done
mkdir -p "$dropin_dir"
if [[ ! -e "$dropin" || "$replace_dropin" -eq 1 ]]; then
  temp="$dropin_dir/.10-hardware-groups.conf.$$"
  printf '%s\n' "$expected_dropin" > "$temp"
  install -o root -g root -m 0644 "$temp" "$dropin"
  rm -f -- "$temp"
fi
replace_managed_file() {
  local source="$1" target="$2" previous="$3" mode="$4"
  if [[ -z "$previous_release" || ! -f "$previous" || -L "$previous" ]] || ! cmp -s "$previous" "$target"; then
    echo "管理fileが検証後に変更されました。上書きしません: $target" >&2; exit 1
  fi
  local temp="$target.tmp.$$"
  [[ ! -e "$temp" && ! -L "$temp" ]] || { echo "管理file staging pathが既にあります: $temp" >&2; exit 1; }
  install -o root -g root -m "$mode" "$source" "$temp"
  mv -Tf -- "$temp" "$target"
}
if [[ ! -e "$clock_dropin" ]]; then
  install -d -o root -g root -m 0755 "$clock_dropin_dir"
  install -o root -g root -m 0644 "$clock_dropin_source" "$clock_dropin"
elif (( replace_clock_dropin )); then
  replace_managed_file "$clock_dropin_source" "$clock_dropin" "$previous_clock_dropin" 0644
fi
if [[ ! -e "$update_dropin" ]]; then
  install -d -o root -g root -m 0755 "$clock_dropin_dir"
  install -o root -g root -m 0644 "$update_dropin_source" "$update_dropin"
elif (( replace_update_dropin )); then
  replace_managed_file "$update_dropin_source" "$update_dropin" "$previous_update_dropin" 0644
fi
if [[ ! -e "$recover_target" ]]; then
  install -d -o root -g root -m 0755 "$(dirname -- "$recover_target")"
  install -o root -g root -m 0755 "$recover_source" "$recover_target"
elif (( replace_recover )); then
  replace_managed_file "$recover_source" "$recover_target" "$previous_recover" 0755
fi
mkdir -p "$web_dropin_dir"
if [[ ! -e "$web_dropin" || "$replace_web_dropin" -eq 1 ]]; then
  temp="$web_dropin_dir/.10-trusted-hosts.conf.$$"
  printf '%s\n' "$expected_web_dropin" > "$temp"
  install -o root -g root -m 0644 "$temp" "$web_dropin"
  rm -f -- "$temp"
fi

temp_link="$opt_root/.current-$project_version-$$"
ln -s "releases/$project_version" "$temp_link"
# current切替を先に済ませ、daemon-reload失敗時もdisk上のunitとreleaseが
# 揃った状態で残るようにする（reloadは次回起動時にも再評価される）。
mv -Tf -- "$temp_link" "$current"
systemctl daemon-reload
printf 'Installed %s. Services are not enabled or started.\n' "$project_version"
