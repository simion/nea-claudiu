from __future__ import annotations

import importlib.metadata
import logging
import os
import sys
from pathlib import Path

import click

from reviewd.colors import BOLD_RED, CYAN, DIM, GREEN, RED, RESET, YELLOW
from reviewd.config import get_provider, load_global_config
from reviewd.daemon import review_single_pr, run_poll_loop
from reviewd.models import CLI, GlobalConfig
from reviewd.state import StateDB

try:
    VERSION = importlib.metadata.version('reviewd')
except importlib.metadata.PackageNotFoundError:
    VERSION = '0.0.0-dev'

CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'reviewd'
CONFIG_PATH = CONFIG_DIR / 'config.yaml'


def _apply_cli_override(config: GlobalConfig, cli: str | None):
    if cli is None:
        return
    cli_enum = CLI(cli)
    config.cli = cli_enum
    for repo in config.repos:
        repo.cli = cli_enum


PROGRESS_LOG_LEVEL = 22
logging.addLevelName(PROGRESS_LOG_LEVEL, 'PROGRESS')

REVIEW_LOG_LEVEL = 25
logging.addLevelName(REVIEW_LOG_LEVEL, 'REVIEW')


class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: DIM,
        PROGRESS_LOG_LEVEL: CYAN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
        REVIEW_LOG_LEVEL: GREEN,
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, '')
        record.levelname = f'{color}{record.levelname:<8}{RESET}'
        if color:
            record.msg = f'{color}{record.msg}{RESET}'
        return super().format(record)


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter('%(asctime)s %(levelname)s %(name)s — %(message)s', datefmt='%H:%M:%S'))
    logging.root.addHandler(handler)
    logging.root.setLevel(level)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


@click.group(invoke_without_command=True)
@click.option('--config', 'config_path', default=None, help='Path to global config file')
@click.pass_context
def main(ctx, config_path: str | None):
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config_path
    click.echo(f'reviewd v{VERSION}')

    if ctx.invoked_subcommand is None:
        path = Path(config_path).expanduser() if config_path else CONFIG_PATH
        if not path.exists():
            ctx.invoke(init)
        else:
            click.echo(ctx.get_help())


UPDATE_CHECK_CACHE = Path(os.environ.get('XDG_CACHE_HOME', '~/.cache')).expanduser() / 'reviewd' / 'latest_version'
UPDATE_CHECK_INTERVAL = 6 * 3600  # seconds


def _check_for_updates():
    try:
        import time

        now = time.time()
        latest = None

        if UPDATE_CHECK_CACHE.exists():
            stat = UPDATE_CHECK_CACHE.stat()
            if now - stat.st_mtime < UPDATE_CHECK_INTERVAL:
                latest = UPDATE_CHECK_CACHE.read_text().strip()

        if latest is None:
            import httpx

            resp = httpx.get('https://pypi.org/pypi/reviewd/json', timeout=2)
            latest = resp.json()['info']['version']
            UPDATE_CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
            UPDATE_CHECK_CACHE.write_text(latest)

        installed = tuple(int(x) for x in VERSION.split('.'))
        remote = tuple(int(x) for x in latest.split('.'))
        if remote > installed:
            exe = sys.executable
            if 'uv/tools' in exe or 'uv\\tools' in exe:
                cmd = 'uv tool upgrade reviewd'
            elif 'pipx' in exe:
                cmd = 'pipx upgrade reviewd'
            else:
                cmd = 'pip install --upgrade reviewd'
            click.echo(f'{YELLOW}Update available: v{VERSION} \u2192 v{latest}  ({cmd}){RESET}')
    except Exception:
        pass


def _ensure_global_config(config_path: str | None) -> Path:
    path = Path(config_path).expanduser() if config_path else CONFIG_PATH
    if not path.exists():
        from reviewd.wizard import run_wizard

        click.echo(f'No config found at {path}. Starting setup wizard...')
        run_wizard()
        if not path.exists():
            raise SystemExit(1)
    return path


@main.command()
@click.option('--sample', is_flag=True, help='Write annotated sample config (non-interactive, for VPS/CI)')
@click.pass_context
def init(ctx, sample: bool):
    """Interactive setup wizard — configure repos, credentials, and AI CLI."""
    from reviewd.wizard import SAMPLE_CONFIG, run_wizard

    if sample:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(SAMPLE_CONFIG)
        click.echo(f'Created sample config at {CONFIG_PATH}')
        click.echo('Edit it to add your tokens and repos.')
        return

    if CONFIG_PATH.exists():
        click.echo(f'Global config already exists at {CONFIG_PATH}. \u2713')
        if not click.confirm('Re-run setup wizard?', default=False):
            return

    run_wizard()


@main.command()
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print reviews without posting')
@click.option('--review-existing', is_flag=True, help='Review unreviewed open PRs on startup')
@click.option('--cli', type=click.Choice(['claude', 'gemini', 'codex']), default=None, help='Override AI CLI')
@click.option('--concurrency', type=int, default=None, help='Max concurrent reviews (default: 4)')
@click.pass_context
def watch(ctx, verbose: bool, dry_run: bool, review_existing: bool, cli: str | None, concurrency: int | None):
    """Start the daemon — polls for new PRs and reviews them."""
    _setup_logging(verbose)
    _check_for_updates()
    _ensure_global_config(ctx.obj['config_path'])
    config = load_global_config(ctx.obj['config_path'])
    _apply_cli_override(config, cli)
    if concurrency is not None:
        config.max_concurrent_reviews = concurrency
    run_poll_loop(config, dry_run=dry_run, review_existing=review_existing, verbose=verbose)


@main.command()
@click.argument('repo')
@click.argument('pr_id', type=int)
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print review without posting')
@click.option('--force', is_flag=True, help='Review even if already reviewed (bypasses cooldown/skip)')
@click.option('--cli', type=click.Choice(['claude', 'gemini', 'codex']), default=None, help='Override AI CLI')
@click.pass_context
def pr(ctx, repo: str, pr_id: int, verbose: bool, dry_run: bool, force: bool, cli: str | None):
    """One-shot review of a specific PR."""
    _setup_logging(verbose)
    _ensure_global_config(ctx.obj['config_path'])
    config = load_global_config(ctx.obj['config_path'])
    _apply_cli_override(config, cli)
    review_single_pr(config, repo, pr_id=pr_id, dry_run=dry_run, force=force)


@main.command(name='ls')
@click.pass_context
def ls_repos(ctx):
    """List watched repos and their open PRs."""
    _setup_logging(False)
    _ensure_global_config(ctx.obj['config_path'])
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
    _ensure_global_config(ctx.obj['config_path'])
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
