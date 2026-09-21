"""Host discovery for ``--list-hosts`` — SSH config and the tailnet."""

from __future__ import annotations

from model_launcher.core.hosts import (
    SshHost,
    available_targets,
    parse_ssh_config,
    tailnet_hosts,
)


def _write(tmp_path, text: str, name: str = "config"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_lists_aliases_with_their_targets(tmp_path):
    path = _write(
        tmp_path,
        """
        Host ai2050cluster
            HostName 10.0.0.1
            User ec2-user
        Host workstation
            HostName lab.example.org
        """,
    )
    hosts = parse_ssh_config(path)
    assert [h.alias for h in hosts] == ["ai2050cluster", "workstation"]
    assert hosts[0].target == "ec2-user@10.0.0.1"
    assert hosts[1].target == "lab.example.org"


def test_patterns_are_not_offered_as_targets(tmp_path):
    """`Host *` configures other hosts; it is not a machine you can connect to."""
    path = _write(
        tmp_path,
        """
        Host *
            ServerAliveInterval 15
        Host !secret cluster
            HostName 10.0.0.2
        Host real
            HostName 10.0.0.3
        """,
    )
    assert [h.alias for h in parse_ssh_config(path)] == ["cluster", "real"]


def test_match_blocks_do_not_leak_into_the_previous_host(tmp_path):
    """A Match block applies conditionally and names no target."""
    path = _write(
        tmp_path,
        """
        Host real
            HostName 10.0.0.3
        Match host *.internal
            User someone
        """,
    )
    hosts = parse_ssh_config(path)
    assert [h.alias for h in hosts] == ["real"]
    assert hosts[0].user is None


def test_multiple_aliases_on_one_line(tmp_path):
    path = _write(tmp_path, "Host short long\n    HostName 10.0.0.4\n")
    hosts = parse_ssh_config(path)
    assert [h.alias for h in hosts] == ["short", "long"]
    assert all(h.hostname == "10.0.0.4" for h in hosts)


def test_included_files_are_followed(tmp_path):
    _write(tmp_path, "Host extra\n    HostName 10.0.0.9\n", name="extra.conf")
    path = _write(
        tmp_path, f"Include {tmp_path}/extra.conf\nHost main\n    HostName 10.0.0.8\n"
    )
    assert [h.alias for h in parse_ssh_config(path)] == ["extra", "main"]


def test_include_cycle_terminates(tmp_path):
    """A self-including config must not hang the CLI."""
    path = tmp_path / "config"
    path.write_text(f"Include {path}\nHost main\n    HostName 10.0.0.8\n")
    assert [h.alias for h in parse_ssh_config(path)] == ["main"]


def test_key_equals_value_form(tmp_path):
    path = _write(tmp_path, "Host eq\n    HostName=10.0.0.5\n    User=root\n")
    assert parse_ssh_config(path)[0].target == "root@10.0.0.5"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    path = _write(
        tmp_path,
        "# a comment\n\nHost real  # trailing\n    HostName 10.0.0.6\n",
    )
    assert [h.alias for h in parse_ssh_config(path)] == ["real"]


def test_missing_config_is_not_an_error(tmp_path):
    """Having no SSH config is a normal state, not a failure."""
    assert parse_ssh_config(tmp_path / "nope") == []


# --- tailnet ---------------------------------------------------------------

SNAPSHOT = {
    "User": {
        "1": {"LoginName": "marina@ersilia.io"},
        "2": {"LoginName": "arnau@ersilia.io"},
    },
    "Self": {
        "HostName": "marina-nuc",
        "DNSName": "marina-nuc.tail0.ts.net.",
        "OS": "linux",
        "Online": True,
        "UserID": 1,
    },
    "Peer": {
        # our own second machine — same owner as Self, no tags
        "a": {
            "HostName": "marina-macbook-pro",
            "OS": "macOS",
            "Online": True,
            "UserID": 1,
        },
        # a colleague's personal laptop — real, online, but not ours to enter
        "b": {"HostName": "arnau-nuc", "OS": "linux", "Online": True, "UserID": 2},
        # shared dev infra: tagged, so it counts regardless of who owns it
        "c": {
            "HostName": "nebula",
            "OS": "linux",
            "Online": False,
            "UserID": 2,
            "Tags": ["tag:dev"],
        },
        "d": {"HostName": "gemma-iphone", "OS": "iOS", "Online": True, "UserID": 2},
        "e": {"HostName": "a36", "OS": "android", "Online": True, "UserID": 1},
    },
}


def test_phones_and_tablets_are_not_launch_targets():
    """They only consume the tailnet; they cannot run a model."""
    names = [h.name for h in tailnet_hosts(SNAPSHOT)]
    assert "gemma-iphone" not in names
    assert "a36" not in names


def test_this_machine_comes_first_and_is_marked():
    hosts = tailnet_hosts(SNAPSHOT)
    assert hosts[0].name == "marina-nuc"
    assert hosts[0].is_self
    assert hosts[0].status == "this machine"


def test_offline_machines_are_listed_not_hidden():
    """'There but down' is more useful than silence."""
    nebula = next(h for h in tailnet_hosts(SNAPSHOT) if h.name == "nebula")
    assert nebula.status == "offline"
    assert nebula.tags == ("tag:dev",)


def test_own_machines_are_launch_targets():
    """A second device under the same tailnet account is ours to enter."""
    names = [h.name for h in tailnet_hosts(SNAPSHOT)]
    assert "marina-macbook-pro" in names


def test_colleagues_personal_devices_are_excluded():
    """Online and real, but we have no login there — it would be a guaranteed
    permission-denied if offered as a target."""
    names = [h.name for h in tailnet_hosts(SNAPSHOT)]
    assert "arnau-nuc" not in names


def test_tagged_devices_are_included_regardless_of_owner():
    """Tags replace personal ownership — a tagged box is shared team infra."""
    names = [h.name for h in tailnet_hosts(SNAPSHOT)]
    assert "nebula" in names


def test_peers_are_sorted_after_self():
    assert [h.name for h in tailnet_hosts(SNAPSHOT)] == [
        "marina-nuc",
        "marina-macbook-pro",
        "nebula",
    ]


def test_owner_is_resolved_from_the_user_map():
    nebula = next(h for h in tailnet_hosts(SNAPSHOT) if h.name == "nebula")
    assert nebula.owner == "arnau@ersilia.io"


def test_no_tailnet_is_not_an_error():
    """Not being on a tailnet is a normal state."""
    assert tailnet_hosts({}) == []


def test_no_self_owner_excludes_every_untagged_peer():
    """With no resolvable owner for Self, only tagged peers can be trusted."""
    snapshot = {**SNAPSHOT, "Self": {**SNAPSHOT["Self"], "UserID": 999}}
    names = [h.name for h in tailnet_hosts(snapshot)]
    assert names == ["marina-nuc", "nebula"]


# --- merged view -----------------------------------------------------------


def test_both_sources_are_merged():
    targets = available_targets(
        ssh=[SshHost("cluster", "10.0.0.1", "ec2-user")],
        tailnet=tailnet_hosts(SNAPSHOT),
    )
    assert [t.name for t in targets] == [
        "cluster",
        "marina-nuc",
        "marina-macbook-pro",
        "nebula",
    ]
    assert targets[0].via == "ssh"
    assert targets[1].via == "tailscale"


def test_a_name_in_both_sources_is_listed_once():
    """An explicit ~/.ssh/config block is a deliberate statement; it wins."""
    targets = available_targets(
        ssh=[SshHost("nebula", "10.9.9.9", "root")], tailnet=tailnet_hosts(SNAPSHOT)
    )
    matches = [t for t in targets if t.name == "nebula"]
    assert len(matches) == 1
    assert matches[0].via == "ssh+tailscale"
    assert matches[0].detail == "root@10.9.9.9"


def test_offline_targets_are_flagged_unreachable():
    targets = available_targets(ssh=[], tailnet=tailnet_hosts(SNAPSHOT))
    nebula = next(t for t in targets if t.name == "nebula")
    assert nebula.reachable is False
