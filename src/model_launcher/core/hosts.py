"""Discovering the machines this user can reach.

``--host`` takes a name that ``ssh`` resolves. Two things make a name
resolvable, and we list both:

* an entry in ``~/.ssh/config`` — how the AWS cluster is reached;
* a device on the Ersilia tailnet — how the workstations are reached, via
  Tailscale SSH and MagicDNS, with no ``~/.ssh/config`` entry at all.

"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set

#: Depth limit for ``Include`` chains, so a cyclic include cannot hang the CLI.
MAX_INCLUDE_DEPTH = 8

#: Tailnet devices that only *consume* the network. They never run code, so they
#: are never launch targets. Classified by Tailscale's own OS field.
CONSUMER_OS = frozenset({"ios", "android", "androidtv", "tvos"})

#: A wedged `tailscaled` must not hang the CLI; listing hosts is meant to be the
#: fast answer you reach for when something else is already broken.
TAILSCALE_TIMEOUT = 5.0


@dataclass(frozen=True)
class SshHost:
    """One connectable alias from the SSH configuration."""

    alias: str
    hostname: Optional[str] = None
    user: Optional[str] = None

    @property
    def target(self) -> str:
        """Human-readable destination, e.g. ``ec2-user@10.0.0.1``."""
        host = self.hostname or "(from defaults)"
        return f"{self.user}@{host}" if self.user else host


def ssh_config_path() -> Path:
    """Return the path to the user's SSH client configuration."""
    return Path(os.path.expanduser("~/.ssh/config"))


def _is_pattern(alias: str) -> bool:
    """Is this a match pattern rather than a nameable host?"""
    return any(char in alias for char in "*?!")


def _resolve_include(argument: str, base: Path) -> List[Path]:
    """Expand one ``Include`` argument into concrete files.

    Relative paths are resolved against ``~/.ssh``, as ``ssh`` itself does.
    """
    expanded = os.path.expanduser(argument)
    if not os.path.isabs(expanded):
        expanded = str(base / expanded)
    return [Path(match) for match in sorted(glob.glob(expanded))]


def parse_ssh_config(path: Optional[Path] = None, _depth: int = 0) -> List[SshHost]:
    """Parse an SSH config file into its connectable host entries.

    Parameters
    ----------
    path : Path, optional
        File to read. Defaults to :func:`ssh_config_path`.
    _depth : int
        Internal recursion guard for ``Include`` directives.

    Returns
    -------
    list of SshHost
        Hosts in the order they appear, without duplicates. An unreadable or
        missing file yields an empty list rather than raising: not having an SSH
        config is a normal state, not an error.
    """
    path = path or ssh_config_path()
    if _depth > MAX_INCLUDE_DEPTH:
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []

    base = path.parent
    hosts: List[SshHost] = []
    seen: Set[str] = set()
    aliases: List[str] = []
    hostname: Optional[str] = None
    user: Optional[str] = None

    def flush() -> None:
        for alias in aliases:
            if alias in seen:
                continue
            seen.add(alias)
            hosts.append(SshHost(alias=alias, hostname=hostname, user=user))

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # `Key value` and `Key=value` are both valid.
        key, _, value = line.replace("=", " ", 1).partition(" ")
        key = key.lower()
        value = value.strip()
        if not value:
            continue

        if key == "host":
            flush()
            aliases = [a for a in value.split() if not _is_pattern(a)]
            hostname = user = None
        elif key == "match":
            # A Match block applies conditionally; it names no target.
            flush()
            aliases = []
            hostname = user = None
        elif key == "include":
            for included in _resolve_include(value, base):
                for host in parse_ssh_config(included, _depth + 1):
                    if host.alias not in seen:
                        seen.add(host.alias)
                        hosts.append(host)
        elif key == "hostname" and aliases:
            hostname = value
        elif key == "user" and aliases:
            user = value

    flush()
    return hosts


# ---------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TailnetHost:
    """One device on the tailnet that could run models."""

    name: str
    os: str = ""
    online: bool = False
    owner: str = ""
    tags: Sequence[str] = ()
    is_self: bool = False

    @property
    def status(self) -> str:
        """One-word reachability, for display."""
        if self.is_self:
            return "this machine"
        return "online" if self.online else "offline"


def _tailscale_status_json(timeout: float = TAILSCALE_TIMEOUT) -> Optional[dict]:
    """Return one ``tailscale status --json`` snapshot, or None.

    Every failure mode — no Tailscale installed, daemon not running, wedged,
    malformed output — collapses to None. Not being on a tailnet is a normal
    state, not an error.
    """
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def _node_to_host(node: dict, users: dict, is_self: bool) -> Optional[TailnetHost]:
    """Convert one Tailscale node to a launch target, or None if it is not one."""
    name = (node.get("HostName") or "") or (node.get("DNSName") or "").split(".")[0]
    if not name:
        return None
    node_os = (node.get("OS") or "").strip()
    if node_os.lower() in CONSUMER_OS:
        return None
    owner = (users.get(str(node.get("UserID"))) or {}).get("LoginName", "")
    return TailnetHost(
        name=name,
        os=node_os,
        online=bool(node.get("Online")),
        owner=owner,
        tags=tuple(node.get("Tags") or ()),
        is_self=is_self,
    )


def _has_login(host: TailnetHost, self_owner: str) -> bool:
    """Is this a machine we can actually log into?

    Tailscale tells us who *owns* a device, not who has a shell account on it.
    Two ownership shapes do imply a login, though: a device owned by the same
    tailnet account as us (our own laptop, our own NUC), and a *tagged* device —
    tags replace personal ownership in Tailscale's model, and by the tailnet's
    own convention (see the Ersilia tailnet README) a tagged machine is shared
    infrastructure everyone on the team can reach, e.g. ``tag:dev``.

    Anything else is a colleague's personal device: real, online, and not ours
    to enter. Listing it as a ``--host`` target would just be a guaranteed
    permission-denied.

    This is a heuristic, not a read of the tailnet's actual SSH ACLs — the
    ACLs are the ground truth and could disagree with it in either direction.
    """
    if host.tags:
        return True
    return bool(self_owner) and host.owner == self_owner


def tailnet_hosts(snapshot: Optional[dict] = None) -> List[TailnetHost]:
    """List tailnet devices we can actually launch models on.

    Parameters
    ----------
    snapshot : dict, optional
        A parsed ``tailscale status --json`` document. Fetched if omitted.

    Returns
    -------
    list of TailnetHost
        This machine first, then the rest by name. Excludes phones and
        tablets (never launch targets) and colleagues' personal machines we
        have no login on (see :func:`_has_login`). Offline machines are kept,
        because "it is there but down" is a different and more useful answer
        than silence.
    """
    snapshot = snapshot if snapshot is not None else _tailscale_status_json()
    if not snapshot:
        return []

    users = snapshot.get("User") or {}
    hosts: List[TailnetHost] = []

    own = _node_to_host(snapshot.get("Self") or {}, users, is_self=True)
    if own:
        hosts.append(own)
    self_owner = own.owner if own else ""

    peers = [
        host
        for peer in (snapshot.get("Peer") or {}).values()
        if (host := _node_to_host(peer, users, is_self=False)) is not None
        and _has_login(host, self_owner)
    ]
    hosts.extend(sorted(peers, key=lambda host: host.name))
    return hosts


# ---------------------------------------------------------------------------
# Unified view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A machine that can be named with ``--host``, however it is reached."""

    name: str
    via: str
    detail: str = ""
    status: str = ""
    reachable: bool = True


def available_targets(
    ssh: Optional[List[SshHost]] = None,
    tailnet: Optional[List[TailnetHost]] = None,
) -> List[Target]:
    """Merge both sources into one list of things ``--host`` will accept.

    A name present in both is reported once, as ``ssh+tailscale``: the SSH entry
    wins for connection details because an explicit ``~/.ssh/config`` block is a
    deliberate statement about how to reach that machine.
    """
    ssh = parse_ssh_config() if ssh is None else ssh
    tailnet = tailnet_hosts() if tailnet is None else tailnet

    by_tailnet = {host.name: host for host in tailnet}
    targets: List[Target] = []
    named: Set[str] = set()

    for host in ssh:
        also = host.alias in by_tailnet
        named.add(host.alias)
        targets.append(
            Target(
                name=host.alias,
                via="ssh+tailscale" if also else "ssh",
                detail=host.target,
                status=by_tailnet[host.alias].status if also else "",
                reachable=by_tailnet[host.alias].online if also else True,
            )
        )

    for host in tailnet:
        if host.name in named:
            continue
        detail = host.os or "?"
        if host.tags:
            detail = f"{detail} · {', '.join(host.tags)}"
        targets.append(
            Target(
                name=host.name,
                via="tailscale",
                detail=detail,
                status=host.status,
                reachable=host.online or host.is_self,
            )
        )
    return targets
