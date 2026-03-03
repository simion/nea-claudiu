from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from reviewd.models import (
    CLI,
    GithubConfig,
    GlobalConfig,
    ProjectConfig,
    RepoConfig,
)
from reviewd.providers.base import GitProvider

ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def _resolve_env_vars(value: str) -> str:
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f'Environment variable {var_name} is not set')
        return env_value

    return ENV_VAR_PATTERN.sub(replacer, value)


def _parse_bitbucket_tokens(data: dict) -> dict[str, str]:
    return {str(workspace): _resolve_env_vars(str(token)) for workspace, token in data.items()}


def _parse_github_config(data: dict) -> GithubConfig:
    return GithubConfig(
        token=_resolve_env_vars(str(data['token'])),
    )


def _parse_cli(value: str, repo_name: str | None = None) -> CLI:
    value = str(value).strip()
    if ' ' in value:
        context = f' for repo "{repo_name}"' if repo_name else ''
        raise ValueError(
            f'Invalid cli value{context}: "{value}". '
            f'Use cli_args for extra arguments (e.g. cli_args: ["{value.split(maxsplit=1)[1]}"])'
        )
    return CLI(value)


def load_global_config(path: str | Path | None = None) -> GlobalConfig:
    if path is None:
        path = Path('~/.config/reviewd/config.yaml').expanduser()
    else:
        path = Path(path).expanduser()

    with open(path) as f:
        data = yaml.safe_load(f)

    global_bb = _parse_bitbucket_tokens(data['bitbucket']) if 'bitbucket' in data else {}

    global_gh = None
    if 'github' in data:
        global_gh = _parse_github_config(data['github'])

    global_cli = _parse_cli(data.get('cli', 'claude'))

    repos = []
    for repo_data in data.get('repos', []):
        repo_gh = None
        if 'github' in repo_data:
            repo_gh = _parse_github_config(repo_data['github'])

        repo_cli = _parse_cli(repo_data['cli'], repo_data['name']) if 'cli' in repo_data else global_cli
        repos.append(
            RepoConfig(
                name=repo_data['name'],
                path=str(Path(repo_data['path']).expanduser()),
                provider=repo_data['provider'],
                repo_slug=repo_data.get('repo_slug'),
                workspace=repo_data.get('workspace'),
                github=repo_gh,
                cli=repo_cli,
                model=repo_data.get('model', data.get('model')),
            )
        )

    state_db = data.get('state_db', '~/.local/share/reviewd/state.db')
    state_db = str(Path(_resolve_env_vars(state_db)).expanduser())

    return GlobalConfig(
        repos=repos,
        bitbucket=global_bb,
        github=global_gh,
        state_db=state_db,
        cli=global_cli,
        model=data.get('model'),
        cli_args=data.get('cli_args', []),
        instructions=data.get('instructions'),
        skip_title_patterns=data.get('skip_title_patterns', ['[no-review]', '[wip]', '[no-claudiu]']),
        skip_authors=data.get('skip_authors', []),
        poll_interval_seconds=data.get('poll_interval_seconds', 60),
        review_title=data.get('review_title', "Code Review by Nea' ~~Caisă~~ Claudiu"),
        footer=data.get(
            'footer',
            'Automated review by [reviewd](https://github.com/simion/reviewd). '
            'Findings are AI-generated — use your judgment.',
        ),
    )


def load_project_config(repo_path: str | Path, global_config: GlobalConfig) -> ProjectConfig:
    config_path = Path(repo_path) / '.reviewd.yaml'
    data = {}
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    # Merge instructions: global + per-project
    parts = []
    if global_config.instructions:
        parts.append(global_config.instructions.strip())
    if data.get('instructions'):
        parts.append(data['instructions'].strip())
    # Backwards compat: support old guidelines/explore fields
    if data.get('guidelines'):
        parts.append(data['guidelines'].strip())
    if data.get('explore'):
        parts.append(data['explore'].strip())
    instructions = '\n\n'.join(parts) if parts else None

    return ProjectConfig(
        instructions=instructions,
        test_commands=data.get('test_commands', []),
        inline_comments_for=data.get('inline_comments_for', ['critical']),
        max_inline_comments=data.get('max_inline_comments'),
        skip_severities=data.get('skip_severities', []),
        show_overview=data.get('show_overview', False),
        min_diff_lines=data.get('min_diff_lines', 0),
        min_diff_lines_update=data.get('min_diff_lines_update', 5),
        review_cooldown_minutes=data.get('review_cooldown_minutes', 0),
        approve_if_no_critical=data.get('approve_if_no_critical', False),
        critical_task=data.get('critical_task', False),
        critical_task_message=data.get('critical_task_message', ProjectConfig.critical_task_message),
    )


def resolve_bitbucket_config(global_config: GlobalConfig, repo_config: RepoConfig) -> tuple[str, str]:
    workspace = repo_config.workspace
    if not workspace:
        raise ValueError(f'Repo "{repo_config.name}" is a bitbucket repo but has no workspace specified')
    token = global_config.bitbucket.get(workspace)
    if not token:
        raise ValueError(f'No bitbucket auth_token found for workspace "{workspace}" (repo "{repo_config.name}")')
    return workspace, token


def resolve_github_config(global_config: GlobalConfig, repo_config: RepoConfig) -> GithubConfig:
    if repo_config.github is not None:
        return repo_config.github
    if global_config.github is not None:
        return global_config.github
    raise ValueError(f'No github config found for repo "{repo_config.name}"')


def get_provider(global_config: GlobalConfig, repo_config: RepoConfig) -> GitProvider:
    if repo_config.provider == 'github':
        from reviewd.providers.github import GithubProvider

        config = resolve_github_config(global_config, repo_config)
        return GithubProvider(config)

    from reviewd.providers.bitbucket import BitbucketProvider

    workspace, token = resolve_bitbucket_config(global_config, repo_config)
    return BitbucketProvider(workspace, token)
