from __future__ import annotations

import importlib.resources
import logging
import shutil
import sys
from pathlib import Path

import click

from reviewd.config import get_provider, load_global_config
from reviewd.daemon import review_single_pr, run_poll_loop
from reviewd.models import CLI, GlobalConfig
from reviewd.state import StateDB

CONFIG_DIR = Path('~/.config/reviewd').expanduser()
CONFIG_PATH = CONFIG_DIR / 'config.yaml'


def _apply_cli_override(config: GlobalConfig, cli: str | None):
    if cli is None:
        return
    cli_enum = CLI(cli)
    config.cli = cli_enum
    for repo in config.repos:
        repo.cli = cli_enum


REVIEW_LOG_LEVEL = 25
logging.addLevelName(REVIEW_LOG_LEVEL, 'REVIEW')


class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: '\033[2m',  # dim
        logging.WARNING: '\033[33m',  # yellow
        logging.ERROR: '\033[31m',  # red
        logging.CRITICAL: '\033[1;31m',  # bold red
        REVIEW_LOG_LEVEL: '\033[32m',  # green
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelno, '')
        record.levelname = f'{color}{record.levelname:<8}{self.RESET}'
        if color:
            record.msg = f'{color}{record.msg}{self.RESET}'
        return super().format(record)


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter('%(asctime)s %(levelname)s %(name)s — %(message)s', datefmt='%H:%M:%S'))
    logging.root.addHandler(handler)
    logging.root.setLevel(level)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


@click.group()
@click.option('--config', 'config_path', default=None, help='Path to global config file')
@click.pass_context
def main(ctx, config_path: str | None):
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config_path


@main.command()
@click.pass_context
def init(ctx):
    """Create config file at ~/.config/reviewd/config.yaml."""
    if CONFIG_PATH.exists():
        click.echo(f'Config already exists: {CONFIG_PATH}')
        if not click.confirm('Overwrite?', default=False):
            return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    example = importlib.resources.files('reviewd').joinpath('config.example.yaml')
    shutil.copy2(str(example), CONFIG_PATH)
    click.echo(f'Created {CONFIG_PATH}')
    click.echo('Edit it to add your provider credentials and repos.')


@main.command()
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print reviews without posting')
@click.option('--review-existing', is_flag=True, help='Review unreviewed open PRs on startup')
@click.option('--cli', type=click.Choice(['claude', 'gemini']), default=None, help='Override AI CLI for all repos')
@click.pass_context
def watch(ctx, verbose: bool, dry_run: bool, review_existing: bool, cli: str | None):
    """Start the daemon — polls for new PRs and reviews them."""
    _setup_logging(verbose)
    config = load_global_config(ctx.obj['config_path'])
    _apply_cli_override(config, cli)
    run_poll_loop(config, dry_run=dry_run, review_existing=review_existing, verbose=verbose)


@main.command()
@click.argument('repo')
@click.argument('pr_id', type=int)
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print review without posting')
@click.option('--force', is_flag=True, help='Review even if already reviewed (bypasses draft/skip)')
@click.option('--cli', type=click.Choice(['claude', 'gemini']), default=None, help='Override AI CLI')
@click.pass_context
def pr(ctx, repo: str, pr_id: int, verbose: bool, dry_run: bool, force: bool, cli: str | None):
    """One-shot review of a specific PR."""
    _setup_logging(verbose)
    config = load_global_config(ctx.obj['config_path'])
    _apply_cli_override(config, cli)
    review_single_pr(config, repo, pr_id=pr_id, dry_run=dry_run, force=force)


@main.command(name='ls')
@click.pass_context
def ls_repos(ctx):
    """List watched repos and their open PRs."""
    _setup_logging(False)
    config = load_global_config(ctx.obj['config_path'])
    state_db = StateDB(config.state_db)
    try:
        for repo_config in config.repos:
            provider_name = repo_config.provider or 'bitbucket'
            click.echo(f'\n{repo_config.name}  ({provider_name}, {repo_config.cli.value})')
            try:
                provider = get_provider(config, repo_config)
                prs = provider.list_open_prs(repo_config.slug)
                if not prs:
                    click.echo('  No open PRs')
                    continue
                for pr in prs:
                    reviewed = state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit)
                    marker = '\u2713' if reviewed else '\u2022'
                    click.echo(f'  {marker} #{pr.pr_id}  {pr.title}  ({pr.author})')
            except Exception as e:
                click.echo(f'  Error: {e}')
    finally:
        state_db.close()
    click.echo()
    click.echo('To review a PR:  reviewd pr <repo> <id>')
    click.echo('To review a PR (dry run):  reviewd pr <repo> <id> --dry-run')


@main.command()
@click.argument('repo')
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--limit', default=20, help='Number of recent reviews to show')
@click.pass_context
def status(ctx, repo: str, verbose: bool, limit: int):
    """Show review history for a repo."""
    _setup_logging(verbose)
    config = load_global_config(ctx.obj['config_path'])
    state_db = StateDB(config.state_db)
    try:
        history = state_db.get_review_history(repo, limit=limit)
        if not history:
            click.echo(f'No review history for {repo}')
            return
        for row in history:
            status_str = row['status']
            pr = row['pr_id']
            commit = row['source_commit'][:8]
            ts = row['created_at']
            err = row.get('error_message', '')
            line = f'PR #{pr}  {commit}  {status_str:<10}  {ts}'
            if err:
                line += f'  error: {err}'
            click.echo(line)
    finally:
        state_db.close()
