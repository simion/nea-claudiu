from __future__ import annotations

from abc import ABC, abstractmethod

from reviewd.models import PRInfo


class GitProvider(ABC):
    @abstractmethod
    def list_open_prs(self, repo_slug: str) -> list[PRInfo]: ...

    @abstractmethod
    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo: ...

    @abstractmethod
    def post_comment(
        self,
        repo_slug: str,
        pr_id: int,
        body: str,
        *,
        file_path: str | None = None,
        line: int | None = None,
        end_line: int | None = None,
        source_commit: str | None = None,
    ) -> int: ...

    @abstractmethod
    def delete_comment(self, repo_slug: str, pr_id: int, comment_id: int) -> bool: ...

    @abstractmethod
    def approve_pr(self, repo_slug: str, pr_id: int) -> None: ...
