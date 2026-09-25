"""Assemble the ``model-launcher`` command-line interface.

Connection options live on the group rather than on each command, because every
command talks to the same target machine::

    model-launcher --host ai2050cluster check

Running the group with no subcommand opens the dashboard, which is what the
tool is for most of the time.
"""

from __future__ import annotations

import click
from rich_click import RichGroup

from .. import __version__
from ..branding import console, style_help
from ..core.hosts import available_targets
from .commands.check import check
from .commands.tui import tui
from .render import no_targets_message, targets_table

style_help()


def _list_hosts(ctx, _param, value):
    """Print the machines this user can reach, then exit.

    Eager and standalone, like ``--version``: it answers "what can I connect
    to?" before any connection is attempted, so it still works when the target
    you were about to name is down.
    """
    if not value or ctx.resilient_parsing:
        return
    out = console()
    targets = available_targets()
    if targets:
        out.print(targets_table(targets))
    else:
        out.print(no_targets_message())
    ctx.exit()


@click.group(cls=RichGroup, invoke_without_command=True)
@click.version_option(__version__, prog_name="model-launcher")
@click.option(
    "--list-hosts",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_list_hosts,
    help="List the machines available as --host targets (SSH config and "
    "tailnet), then exit.",
)
@click.option(
    "--host",
    default=None,
    help="SSH alias of the machine to drive (default: $SCHEDULER_HOST, else local).",
)
@click.option(
    "--ctl",
    default=None,
    help="Path to sched-ctl.sh on the target (default: $SCHEDULER_CTL; over SSH, "
    "the copy the running driver uses; locally, the deployed copy, else the one "
    "packaged with this client).",
)
@click.option(
    "--log-dir",
    default=None,
    help="Scheduler LOG_DIR to inspect (default: $LOG_DIR; over SSH, the running "
    "driver's; else the ctl default).",
)
@click.option(
    "--queue-file",
    default=None,
    help="Queue file to edit (default: discovered from the running driver).",
)
@click.option("--s3-bucket", default=None, help="Override S3_BUCKET for ctl calls.")
@click.option(
    "--ssh-opt",
    multiple=True,
    metavar="OPT",
    help="Extra argument for ssh; repeat once per word, e.g. "
    "--ssh-opt -p --ssh-opt 2222.",
)
@click.option(
    "--who",
    default=None,
    help="Your name, recorded on the target's audit log and cancellation notes "
    "(default: $SCHEDULER_WHO, else your local username).",
)
@click.pass_context
def cli(ctx, host, ctl, log_dir, queue_file, s3_bucket, ssh_opt, who):
    """Launch and steer Ersilia models over large chemical libraries.

    With no subcommand, opens the dashboard.
    """
    ctx.ensure_object(dict)
    ctx.obj.update(
        host=host,
        ctl=ctl,
        log_dir=log_dir,
        queue_file=queue_file,
        s3_bucket=s3_bucket,
        ssh_opts=list(ssh_opt),
        who=who,
    )
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui)


cli.add_command(tui)
cli.add_command(check)

__all__ = ["cli"]
